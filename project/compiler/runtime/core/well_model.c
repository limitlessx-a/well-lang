#include "wellrt.h"
#include "well_internal.h"

#include <stdlib.h>

/* Model facade: cache + pluggable Store backend (MemoryStore by default). */
static WellStore *g_model_store = NULL;
static int g_model_inited = 0;

#define ACT_BUCKETS 1024
typedef struct ActEntry {
    long long oid;
    long long last_active_ms;
    int exec_ref;
    struct ActEntry *next;
} ActEntry;

static ActEntry *g_act[ACT_BUCKETS];
static WellLock g_act_lock = 0;
static long long g_last_cleanup_ms = 0;
static int g_activity_enabled = -1;

/* Idle oid reclamation is ON by default (60s); WELL_MODEL_TTL_MS=0 disables it. */
#define MODEL_DEFAULT_TTL_MS 60000LL

void well_model_activity_enable(void) {
    g_activity_enabled = 1;
}

static int activity_enabled(void) {
    if (g_activity_enabled < 0) {
        const char *env = getenv("WELL_MODEL_TTL_MS");
        if (!env || !env[0]) g_activity_enabled = 1;      /* default: enabled */
        else g_activity_enabled = (atoi(env) > 0) ? 1 : 0; /* explicit 0/negative: off */
    }
    return g_activity_enabled;
}

static long long g_model_ttl_ms = 0;
static long long getenv_ttl_cached(void) {
    if (g_model_ttl_ms == 0) {
        const char *env = getenv("WELL_MODEL_TTL_MS");
        g_model_ttl_ms = (env && env[0]) ? (long long)atoi(env) : MODEL_DEFAULT_TTL_MS;
    }
    return g_model_ttl_ms;
}

static unsigned act_bucket(long long oid) {
    unsigned long long x = (unsigned long long)oid;
    x ^= x >> 33;
    x *= 0xff51afd7ed558ccdULL;
    x ^= x >> 33;
    return (unsigned)(x & (ACT_BUCKETS - 1));
}

static ActEntry *act_get_locked(long long oid, long long now) {
    unsigned b = act_bucket(oid);
    ActEntry *a;
    for (a = g_act[b]; a; a = a->next) {
        if (a->oid == oid) return a;
    }
    a = (ActEntry *)malloc(sizeof(ActEntry));
    if (!a) well_panic("out of memory (activity map)");
    a->oid = oid;
    a->last_active_ms = now;
    a->exec_ref = 0;
    a->next = g_act[b];
    g_act[b] = a;
    return a;
}

static ActEntry *act_find_locked(long long oid) {
    ActEntry *a;
    for (a = g_act[act_bucket(oid)]; a; a = a->next)
        if (a->oid == oid) return a;
    return NULL;
}

static void act_remove_locked(ActEntry *a) {
    ActEntry **pp = &g_act[act_bucket(a->oid)];
    while (*pp) {
        if (*pp == a) {
            *pp = a->next;
            free(a);
            return;
        }
        pp = &(*pp)->next;
    }
}

static void act_touch_locked(long long oid, long long now) {
    act_get_locked(oid, now)->last_active_ms = now;
}

static void act_pin_locked(long long oid) {
    act_get_locked(oid, well_mono_ms())->exec_ref++;
}

static void act_unpin_locked(long long oid) {
    unsigned b = act_bucket(oid);
    ActEntry *a;
    for (a = g_act[b]; a; a = a->next) {
        if (a->oid == oid) {
            if (a->exec_ref > 0) a->exec_ref--;
            break;
        }
    }
}

void well_model_touch_oid(long long oid) {
    long long now;
    if (!activity_enabled()) return;
    now = well_mono_ms();
    well_lock_acquire(&g_act_lock);
    act_touch_locked(oid, now);
    well_lock_release(&g_act_lock);
}

void well_oid_pin(long long oid) {
    well_lock_acquire(&g_act_lock);
    act_pin_locked(oid);
    well_lock_release(&g_act_lock);
}

void well_oid_unpin(long long oid) {
    well_lock_acquire(&g_act_lock);
    act_unpin_locked(oid);
    well_lock_release(&g_act_lock);
}

void well_model_init(void) {
    if (g_model_inited) return;
    g_model_store = well_memory_store_new();
    g_model_inited = 1;
}

void well_model_use_store(WellStore *store) {
    if (g_model_store && g_model_store != store)
        g_model_store->shutdown(g_model_store->self);
    g_model_store = store;
    g_model_inited = 1;
}

void *well_model_get(unsigned int type_id, long long oid) {
    return well_model_find_by_oid(type_id, oid);
}

void *well_model_get_ctx(unsigned int type_id, long long oid, WellCtx *ctx) {
    void *obj;
    if (ctx) well_ctx_pin(ctx, oid);
    obj = well_model_find_by_oid(type_id, oid);
    if (!obj && ctx) well_ctx_unpin(ctx, oid);
    return obj;
}

void *well_model_peek(unsigned int type_id, long long oid) {
    return well_cache_get(type_id, oid);
}

void *well_model_peek_ctx(unsigned int type_id, long long oid, WellCtx *ctx) {
    void *obj;
    if (ctx) well_ctx_pin(ctx, oid);
    obj = well_cache_get(type_id, oid);
    if (!obj && ctx) well_ctx_unpin(ctx, oid);
    return obj;
}

void *well_model_find_by_oid(unsigned int type_id, long long oid) {
    void *obj;
    well_model_init();
    obj = well_cache_get(type_id, oid);
    if (obj) return obj;
    if (!g_model_store || !g_model_store->select) return NULL;
    obj = g_model_store->select(g_model_store->self, type_id, oid);
    if (!obj) return NULL;
    well_cache_set(obj);
    return obj; /* owned reference from Store */
}

void well_model_set(void *obj, WellCtx *ctx) {
    WellObj *o = (WellObj *)obj;
    if (!o) well_panic("Model.set(null)");
    if (o->oid != ctx->oid) well_panic("Model.set: oid mismatch");
    well_model_init();
    if (g_model_store && g_model_store->upsert) {
        if (g_model_store->upsert(g_model_store->self, obj) != 0)
            well_panic("Model.set: store write failed");
    }
    well_cache_set(obj);
}

void well_model_remove(unsigned int type_id, long long oid, WellCtx *ctx) {
    if (ctx->oid != oid) well_panic("Model.remove: oid mismatch");
    well_model_init();
    if (g_model_store && g_model_store->delete_) {
        if (g_model_store->delete_(g_model_store->self, type_id, oid) != 0)
            well_panic("Model.remove: store delete failed");
    }
    well_cache_remove(type_id, oid);
}

#define MODEL_CLEANUP_BATCH_DEFAULT 64
#define MODEL_CLEANUP_BATCH_MAX 256
static int cleanup_batch_size(void) {
    static int cached = -1;
    if (cached < 0) {
        const char *env = getenv("WELL_MODEL_TTL_BATCH");
        int v = (env && env[0]) ? atoi(env) : MODEL_CLEANUP_BATCH_DEFAULT;
        if (v < 1) v = 1;
        if (v > MODEL_CLEANUP_BATCH_MAX) v = MODEL_CLEANUP_BATCH_MAX;
        cached = v;
    }
    return cached;
}

void well_model_cleanup(long long now_ms, long long ttl_ms) {
    long long cand[MODEL_CLEANUP_BATCH_MAX];
    int n = 0, i, b, max;
    if (ttl_ms <= 0) return;
    if (!well_scheduler_cleanup_enter()) return;
    max = cleanup_batch_size();

    /* Phase 1: collect candidate oids (short act lock, read-only scan). */
    well_lock_acquire(&g_act_lock);
    for (b = 0; b < ACT_BUCKETS && n < max; b++) {
        ActEntry *a;
        for (a = g_act[b]; a && n < max; a = a->next) {
            if (a->oid == well_static_owner_oid()) continue;
            if (now_ms - a->last_active_ms < ttl_ms) continue;
            if (a->exec_ref > 0) continue;
            cand[n++] = a->oid;
        }
    }
    well_lock_release(&g_act_lock);

    /* Phase 2: per oid - quarantine, detach objects/cache rows under act lock
     * (blocks new pins/reloads), then free everything without global locks. */
    for (i = 0; i < n; i++) {
        long long oid = cand[i];
        ActEntry *a;
        WellObj *objs;

        well_lock_acquire(&g_act_lock);
        a = act_find_locked(oid);
        if (!a || a->exec_ref > 0 || now_ms - a->last_active_ms < ttl_ms) {
            well_lock_release(&g_act_lock);
            continue;
        }
        if (!well_scheduler_begin_cleanup(oid)) { /* act -> sched order */
            well_lock_release(&g_act_lock);
            continue;
        }
        act_remove_locked(a);
        objs = well_oid_take_objects(oid);   /* registry shard lock */
        well_cache_discard_oid(oid);         /* cache shard locks */
        well_lock_release(&g_act_lock);

        well_force_free_objects(objs);       /* no global locks held */
        well_scheduler_end_cleanup(oid);
    }
    well_scheduler_cleanup_leave();
}

void well_model_cleanup_if_due(void) {
    long long now;
    well_pool_sweep();
    /* env is parsed once (this runs every scheduler iteration) */
    if (g_activity_enabled < 0) (void)activity_enabled();
    if (g_activity_enabled != 1) return;
    now = well_mono_ms();
    if (now - g_last_cleanup_ms < 1000) return;
    g_last_cleanup_ms = now;
    well_model_cleanup(now, (long long)getenv_ttl_cached());
}

void well_model_shutdown(void) {
    int b;
    if (g_model_store) {
        g_model_store->shutdown(g_model_store->self);
        g_model_store = NULL;
    }
    well_cache_shutdown();
    well_lock_acquire(&g_act_lock);
    for (b = 0; b < ACT_BUCKETS; b++) {
        ActEntry *a = g_act[b];
        while (a) {
            ActEntry *next = a->next;
            free(a);
            a = next;
        }
        g_act[b] = NULL;
    }
    well_lock_release(&g_act_lock);
    g_model_inited = 0;
}
