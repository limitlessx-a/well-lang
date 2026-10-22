#include "wellrt.h"
#include "well_internal.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <setjmp.h>

#ifdef _WIN32
#include <windows.h>
#endif

/* ---------- Cache (sharded hash table) ---------- */
typedef struct {
    unsigned int type_id;
    long long oid;
    void *obj;
    int state; /* 0 = empty, 1 = used, -1 = tombstone */
} WellModelEntry;

#define MODEL_SHARD_BITS 6
#define MODEL_SHARDS (1 << MODEL_SHARD_BITS)
#define MODEL_TOMB (-1)
#define MODEL_LOAD_NUM 7
#define MODEL_LOAD_DEN 10

typedef struct {
    WellLock lock;
    WellModelEntry *entries;
    int cap;
    int live;
    int tomb;
} WellModelShard;

static WellModelShard g_model_shards[MODEL_SHARDS];

static unsigned long long model_hash(unsigned int type_id, long long oid) {
    unsigned long long x = (unsigned long long)oid +
                           ((unsigned long long)type_id + 1ULL) * 0x9e3779b97f4a7c15ULL;
    x ^= x >> 30;
    x *= 0xbf58476d1ce4e5b9ULL;
    x ^= x >> 27;
    x *= 0x94d049bb133111ebULL;
    x ^= x >> 31;
    return x ? x : 1ULL;
}

static WellModelShard *model_shard(unsigned int type_id, long long oid) {
    return &g_model_shards[model_hash(type_id, oid) & (MODEL_SHARDS - 1)];
}

static void model_rehash(WellModelShard *sh, int ncap) {
    WellModelEntry *nt = (WellModelEntry *)calloc((size_t)ncap, sizeof(WellModelEntry));
    int i;
    if (!nt) well_panic("out of memory (Cache)");
    for (i = 0; i < sh->cap; i++) {
        if (sh->entries[i].state == 1) {
            unsigned long long mask = (unsigned long long)(ncap - 1);
            unsigned long long h = model_hash(sh->entries[i].type_id, sh->entries[i].oid) & mask;
            while (nt[h].state == 1) h = (h + 1) & mask;
            nt[h] = sh->entries[i];
        }
    }
    free(sh->entries);
    sh->entries = nt;
    sh->cap = ncap;
    sh->tomb = 0;
}

static void model_ensure(WellModelShard *sh, int need) {
    if (sh->cap == 0 ||
        (sh->live + sh->tomb + need) * MODEL_LOAD_DEN >= sh->cap * MODEL_LOAD_NUM) {
        int ncap = 16;
        while (ncap * MODEL_LOAD_NUM < (sh->live + sh->tomb + need) * MODEL_LOAD_DEN)
            ncap *= 2;
        model_rehash(sh, ncap);
    }
}

static int model_find_slot(WellModelShard *sh, unsigned int type_id, long long oid) {
    unsigned long long mask, h;
    if (sh->cap == 0) return -1;
    mask = (unsigned long long)(sh->cap - 1);
    h = model_hash(type_id, oid) & mask;
    for (;;) {
        int s = sh->entries[h].state;
        if (s == 0) return -1;
        if (s == 1 && sh->entries[h].type_id == type_id && sh->entries[h].oid == oid)
            return (int)h;
        h = (h + 1) & mask;
    }
}

void *well_cache_get(unsigned int type_id, long long oid) {
    WellModelShard *sh = model_shard(type_id, oid);
    void *obj = NULL;
    int i;
    well_model_touch_oid(oid);
    well_lock_acquire(&sh->lock);
    i = model_find_slot(sh, type_id, oid);
    if (i >= 0) {
        obj = sh->entries[i].obj;
        well_retain(obj); /* get returns an owned reference under the shard lock */
    }
    well_lock_release(&sh->lock);
    return obj;
}

void well_cache_set(void *obj) {
    WellObj *o = (WellObj *)obj;
    WellModelShard *sh;
    int i;
    if (!o) well_panic("cache set(null)");
    well_model_touch_oid(o->oid);
    sh = model_shard(o->type_id, o->oid);
    well_lock_acquire(&sh->lock);
    if (o->flags & WELL_FLAG_IN_MODEL) {
        i = model_find_slot(sh, o->type_id, o->oid);
        if (i >= 0 && sh->entries[i].obj == o) {
            well_lock_release(&sh->lock);
            return; /* idempotent */
        }
        well_lock_release(&sh->lock);
        well_panic("object already in Model under another key");
    }
    model_ensure(sh, 1);
    i = model_find_slot(sh, o->type_id, o->oid);
    if (i >= 0) {
        WellObj *old = (WellObj *)sh->entries[i].obj;
        if (old != o) {
            old->flags &= ~WELL_FLAG_IN_MODEL;
            sh->entries[i].obj = o;
            sh->entries[i].state = 1;
            o->flags |= WELL_FLAG_IN_MODEL;
            well_retain(o);
            well_release(old);
            well_lock_release(&sh->lock);
            return;
        }
        o->flags |= WELL_FLAG_IN_MODEL;
        well_lock_release(&sh->lock);
        return;
    }
    {
        unsigned long long mask = (unsigned long long)(sh->cap - 1);
        unsigned long long h = model_hash(o->type_id, o->oid) & mask;
        int tomb = -1;
        for (;;) {
            int st = sh->entries[h].state;
            if (st == 0) {
                if (tomb >= 0) h = (unsigned long long)tomb;
                sh->entries[h].type_id = o->type_id;
                sh->entries[h].oid = o->oid;
                sh->entries[h].obj = o;
                sh->entries[h].state = 1;
                sh->live++;
                break;
            }
            if (st == MODEL_TOMB && tomb < 0) tomb = (int)h;
            h = (h + 1) & mask;
        }
    }
    o->flags |= WELL_FLAG_IN_MODEL;
    well_retain(o);
    well_lock_release(&sh->lock);
}

void well_cache_remove(unsigned int type_id, long long oid) {
    WellModelShard *sh;
    int i;
    WellObj *o;
    sh = model_shard(type_id, oid);
    well_lock_acquire(&sh->lock);
    i = model_find_slot(sh, type_id, oid);
    if (i < 0) {
        well_lock_release(&sh->lock);
        return;
    }
    o = (WellObj *)sh->entries[i].obj;
    sh->entries[i].obj = NULL;
    sh->entries[i].state = MODEL_TOMB;
    sh->live--;
    sh->tomb++;
    if (o) {
        o->flags &= ~WELL_FLAG_IN_MODEL;
        well_release(o);
    }
    if (sh->cap && (sh->live + sh->tomb) * MODEL_LOAD_DEN >= sh->cap * MODEL_LOAD_NUM)
        model_rehash(sh, sh->cap);
    well_lock_release(&sh->lock);
}

void well_cache_shutdown(void) {
    int s, i;
    for (s = 0; s < MODEL_SHARDS; s++) {
        WellModelShard *sh = &g_model_shards[s];
        well_lock_acquire(&sh->lock);
        for (i = 0; i < sh->cap; i++) {
            if (sh->entries[i].state == 1 && sh->entries[i].obj) {
                WellObj *o = (WellObj *)sh->entries[i].obj;
                o->flags &= ~WELL_FLAG_IN_MODEL;
                sh->entries[i].state = 0;
                well_release(o);
            }
        }
        free(sh->entries);
        sh->entries = NULL;
        sh->cap = 0;
        sh->live = 0;
        sh->tomb = 0;
        well_lock_release(&sh->lock);
    }
}

int well_cache_evict_oid(long long oid) {
    int s, i, removed = 0;
    for (s = 0; s < MODEL_SHARDS; s++) {
        WellModelShard *sh = &g_model_shards[s];
        well_lock_acquire(&sh->lock);
        for (i = 0; i < sh->cap; i++) {
            WellModelEntry *e = &sh->entries[i];
            if (e->state != 1 || e->oid != oid || !e->obj) continue;
            if (!well_rc_is_one(e->obj)) continue;
            {
                WellObj *o = (WellObj *)e->obj;
                e->obj = NULL;
                e->state = MODEL_TOMB;
                sh->live--;
                sh->tomb++;
                o->flags &= ~WELL_FLAG_IN_MODEL;
                well_release(o);
                removed++;
            }
        }
        if (sh->cap && (sh->live + sh->tomb) * MODEL_LOAD_DEN >= sh->cap * MODEL_LOAD_NUM)
            model_rehash(sh, sh->cap);
        well_lock_release(&sh->lock);
    }
    return removed;
}

int well_cache_evict_oid_all(long long oid) {
    int s, i, removed = 0;
    for (s = 0; s < MODEL_SHARDS; s++) {
        WellModelShard *sh = &g_model_shards[s];
        well_lock_acquire(&sh->lock);
        for (i = 0; i < sh->cap; i++) {
            WellModelEntry *e = &sh->entries[i];
            if (e->state != 1 || e->oid != oid || !e->obj) continue;
            {
                WellObj *o = (WellObj *)e->obj;
                e->obj = NULL;
                e->state = MODEL_TOMB;
                sh->live--;
                sh->tomb++;
                o->flags &= ~WELL_FLAG_IN_MODEL;
                well_release(o);
                removed++;
            }
        }
        if (sh->cap && (sh->live + sh->tomb) * MODEL_LOAD_DEN >= sh->cap * MODEL_LOAD_NUM)
            model_rehash(sh, sh->cap);
        well_lock_release(&sh->lock);
    }
    return removed;
}

/* Language-level Cache API (separate from Model/Store). */
void *well_cache_get_ctx(unsigned int type_id, long long oid, WellCtx *ctx) {
    void *obj;
    if (ctx) well_ctx_pin(ctx, oid);
    obj = well_cache_get(type_id, oid);
    if (!obj && ctx) well_ctx_unpin(ctx, oid);
    return obj;
}

void well_cache_set_ctx(void *obj, WellCtx *ctx) {
    WellObj *o = (WellObj *)obj;
    if (!o) well_panic("Cache.set(null)");
    if (!ctx || o->oid != ctx->oid) well_panic("Cache.set: oid mismatch");
    well_cache_set(obj);
}

/* Remove every Cache row owned by ctx.oid (all types); Store is untouched.
 * Intended as the manual release path when an oid's work is done; TTL cleanup
 * remains the last-resort fallback. */
void well_cache_remove_ctx(WellCtx *ctx) {
    if (ctx) (void)well_cache_evict_oid_all(ctx->oid);
}

int well_cache_discard_oid(long long oid) {
    int s, i, removed = 0;
    for (s = 0; s < MODEL_SHARDS; s++) {
        WellModelShard *sh = &g_model_shards[s];
        well_lock_acquire(&sh->lock);
        for (i = 0; i < sh->cap; i++) {
            WellModelEntry *e = &sh->entries[i];
            if (e->state != 1 || e->oid != oid) continue;
            e->obj = NULL;
            e->state = MODEL_TOMB;
            sh->live--;
            sh->tomb++;
            removed++;
        }
        if (sh->cap && (sh->live + sh->tomb) * MODEL_LOAD_DEN >= sh->cap * MODEL_LOAD_NUM)
            model_rehash(sh, sh->cap);
        well_lock_release(&sh->lock);
    }
    return removed;
}

int well_cache_oid_rows(long long oid) {
    int s, i, n = 0;
    for (s = 0; s < MODEL_SHARDS; s++) {
        WellModelShard *sh = &g_model_shards[s];
        well_lock_acquire(&sh->lock);
        for (i = 0; i < sh->cap; i++) {
            WellModelEntry *e = &sh->entries[i];
            if (e->state == 1 && e->oid == oid) n++;
        }
        well_lock_release(&sh->lock);
    }
    return n;
}

