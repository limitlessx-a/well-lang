#include "wellrt.h"
#include "well_internal.h"

#include <stdlib.h>
#include <string.h>

/* In-memory Store backend: stores encoded rows (not object pointers). */
typedef struct {
    unsigned int type_id;
    long long oid;
    unsigned char *data;
    size_t len;
} MemEntry;

typedef struct {
    WellStore api;
    WellLock lock;
    MemEntry *entries;
    int len;
    int cap;
} MemStore;

typedef struct {
    MemStore *store;
    void **temps;
    int n;
    int cap;
} MemResolveCtx;

static void *mem_select(void *self, unsigned int type_id, long long oid);

static void mem_track(MemResolveCtx *ctx, void *obj) {
    if (!obj) return;
    if (ctx->n == ctx->cap) {
        int ncap = ctx->cap ? ctx->cap * 2 : 8;
        void **nt = (void **)realloc(ctx->temps, (size_t)ncap * sizeof(void *));
        if (!nt) well_panic("out of memory (MemoryStore resolver)");
        ctx->temps = nt;
        ctx->cap = ncap;
    }
    ctx->temps[ctx->n++] = obj;
}

static void *mem_resolve(unsigned int type_id, long long oid, void *user) {
    MemResolveCtx *ctx = (MemResolveCtx *)user;
    void *obj = mem_select(ctx->store, type_id, oid);
    if (obj) mem_track(ctx, obj);
    return obj;
}

static void *mem_select(void *self, unsigned int type_id, long long oid) {
    MemStore *s = (MemStore *)self;
    unsigned char *copy = NULL;
    size_t len = 0;
    int i;
    well_lock_acquire(&s->lock);
    for (i = 0; i < s->len; i++) {
        if (s->entries[i].type_id == type_id && s->entries[i].oid == oid) {
            copy = (unsigned char *)malloc(s->entries[i].len);
            if (!copy) {
                well_lock_release(&s->lock);
                well_panic("out of memory (MemoryStore select)");
            }
            memcpy(copy, s->entries[i].data, s->entries[i].len);
            len = s->entries[i].len;
            break;
        }
    }
    well_lock_release(&s->lock);
    if (!copy) return NULL;
    {
        MemResolveCtx ctx;
        void *obj;
        int k;
        memset(&ctx, 0, sizeof(ctx));
        ctx.store = s;
        obj = well_decode(type_id, oid, copy, len, mem_resolve, &ctx);
        free(copy);
        for (k = 0; k < ctx.n; k++) well_release(ctx.temps[k]);
        free(ctx.temps);
        return obj;
    }
}

static int mem_upsert(void *self, void *obj) {
    MemStore *s = (MemStore *)self;
    WellObj *o = (WellObj *)obj;
    unsigned char *bytes;
    size_t len = 0;
    int i;
    if (!o) return -1;
    bytes = well_encode(obj, &len);
    well_lock_acquire(&s->lock);
    for (i = 0; i < s->len; i++) {
        if (s->entries[i].type_id == o->type_id && s->entries[i].oid == o->oid) {
            free(s->entries[i].data);
            s->entries[i].data = bytes;
            s->entries[i].len = len;
            well_lock_release(&s->lock);
            return 0;
        }
    }
    if (s->len == s->cap) {
        int ncap = s->cap ? s->cap * 2 : 16;
        MemEntry *ne = (MemEntry *)realloc(s->entries, (size_t)ncap * sizeof(MemEntry));
        if (!ne) {
            well_lock_release(&s->lock);
            free(bytes);
            return -1;
        }
        s->entries = ne;
        s->cap = ncap;
    }
    s->entries[s->len].type_id = o->type_id;
    s->entries[s->len].oid = o->oid;
    s->entries[s->len].data = bytes;
    s->entries[s->len].len = len;
    s->len++;
    well_lock_release(&s->lock);
    return 0;
}

static int mem_delete(void *self, unsigned int type_id, long long oid) {
    MemStore *s = (MemStore *)self;
    int i;
    well_lock_acquire(&s->lock);
    for (i = 0; i < s->len; i++) {
        if (s->entries[i].type_id == type_id && s->entries[i].oid == oid) {
            int j;
            free(s->entries[i].data);
            for (j = i; j + 1 < s->len; j++) s->entries[j] = s->entries[j + 1];
            s->len--;
            well_lock_release(&s->lock);
            return 0;
        }
    }
    well_lock_release(&s->lock);
    return 0;
}

static void mem_shutdown(void *self) {
    MemStore *s = (MemStore *)self;
    int i;
    for (i = 0; i < s->len; i++) free(s->entries[i].data);
    free(s->entries);
    free(s);
}

WellStore *well_memory_store_new(void) {
    MemStore *s = (MemStore *)calloc(1, sizeof(MemStore));
    if (!s) well_panic("out of memory (MemoryStore)");
    s->api.self = s;
    s->api.select = mem_select;
    s->api.upsert = mem_upsert;
    s->api.delete_ = mem_delete;
    s->api.shutdown = mem_shutdown;
    return &s->api;
}
