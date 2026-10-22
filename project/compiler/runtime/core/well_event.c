#include "wellrt.h"
#include "well_internal.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <setjmp.h>

/* ---------- subscriptions ---------- */
typedef struct WellSub {
    WellHandlerFn fn;
    unsigned int event_type;
    long long channel;
    int once;
    int active;
    int claimed;
    int owner;
    struct WellSub *next;
} WellSub;

static WellSub *g_subs = NULL;
static WellLock g_subs_lock = 0;

#define WELL_OWNER_MAX 64
static int g_owner_stack[WELL_OWNER_MAX];
static int g_owner_depth = 0;

void well_owner_push(int owner) {
    if (g_owner_depth < WELL_OWNER_MAX)
        g_owner_stack[g_owner_depth++] = owner;
}

void well_owner_pop(void) {
    if (g_owner_depth > 0) g_owner_depth--;
}

int well_owner_current(void) {
    return g_owner_depth > 0 ? g_owner_stack[g_owner_depth - 1] : 0;
}

static void sub_append(WellSub *s) {
    WellSub *tail;
    if (!g_subs) {
        g_subs = s;
        return;
    }
    tail = g_subs;
    while (tail->next) tail = tail->next;
    tail->next = s;
}

void well_on_type(unsigned int event_type, WellHandlerFn fn, int once, long long channel) {
    WellSub *s = (WellSub *)malloc(sizeof(WellSub));
    if (channel < WELL_CHANNEL_ALL) well_panic("invalid channel");
    if (!s) well_panic("out of memory (subscription)");
    s->fn = fn;
    s->event_type = event_type;
    s->channel = channel;
    s->once = once;
    s->active = 1;
    s->claimed = 0;
    s->owner = well_owner_current();
    s->next = NULL;
    well_lock_acquire(&g_subs_lock);
    sub_append(s);
    well_lock_release(&g_subs_lock);
}

void well_off_type(unsigned int event_type, WellHandlerFn fn, long long channel) {
    WellSub *s;
    int owner = well_owner_current();
    well_lock_acquire(&g_subs_lock);
    for (s = g_subs; s; s = s->next) {
        if (s->active && s->owner == owner && s->fn == fn &&
            s->event_type == event_type && s->channel == channel)
            s->active = 0;
    }
    well_lock_release(&g_subs_lock);
}

void well_off_owner(int owner) {
    WellSub *s;
    well_lock_acquire(&g_subs_lock);
    for (s = g_subs; s; s = s->next) {
        if (s->active && s->owner == owner) s->active = 0;
    }
    well_lock_release(&g_subs_lock);
}

/* ---------- per-oid queues + worker pool (V2) ---------- */
typedef struct WellEventNode {
    long long channel;
    void *event;
    int kind; /* 0 = event, 1 = task */
    WellTaskFn task_fn;
    WellTaskDone task_done;
    void *task_arg;
    struct WellEventNode *next;
} WellEventNode;

typedef struct WellOidQueue {
    long long oid;
    WellEventNode *head;
    WellEventNode *tail;
    int scheduled;
    int running;
    struct WellOidQueue *ready_next;
    struct WellOidQueue *ready_prev;
} WellOidQueue;

static WellMutex g_sched_mutex;
static WellCond g_sched_cond;
static int g_sched_inited = 0;
static WellOidQueue **g_qmap = NULL;
static int g_qmap_cap = 0;
static int g_qmap_used = 0;
static int g_qmap_tomb = 0;

/* Per-worker LIFO/FIFO ready deques + global injector. */
#define WELL_MAX_WORKERS 64
typedef struct WellWorkerQueue {
    WellOidQueue *head;
    WellOidQueue *tail;
    int count;
} WellWorkerQueue;

static WellWorkerQueue g_local[WELL_MAX_WORKERS];
static WellOidQueue *g_inject_head = NULL;
static WellOidQueue *g_inject_tail = NULL;
static int g_worker_count = 0;

static long long g_pending = 0;
static int g_stop = 0;
static int g_service_count = 0;
static WellThread g_threads[WELL_MAX_WORKERS];
static int g_thread_count = 0;

#define QMAP_TOMB ((WellOidQueue *)-1)

/* Small object pools for the hot enqueue/dispatch path. They are capped and
 * guarded by g_sched_mutex; leftovers are process-lifetime (bounded). */
#define WELL_NODE_POOL_MAX 256
#define WELL_QUEUE_POOL_MAX 256
static WellEventNode *g_node_pool = NULL;
static int g_node_pool_n = 0;
static WellOidQueue *g_queue_pool = NULL;
static int g_queue_pool_n = 0;

/* Caller holds g_sched_mutex. */
static WellEventNode *node_pool_take(void) {
    WellEventNode *n = g_node_pool;
    if (!n) return NULL;
    g_node_pool = n->next;
    g_node_pool_n--;
    return n;
}

/* Caller holds g_sched_mutex. */
static void node_pool_put(WellEventNode *n) {
    if (!n) return;
    if (g_node_pool_n >= WELL_NODE_POOL_MAX) {
        free(n);
        return;
    }
    n->event = NULL;
    n->next = g_node_pool;
    g_node_pool = n;
    g_node_pool_n++;
}

/* Caller holds g_sched_mutex. */
static WellOidQueue *queue_pool_take(void) {
    WellOidQueue *q = g_queue_pool;
    if (!q) return NULL;
    g_queue_pool = q->ready_next;
    g_queue_pool_n--;
    memset(q, 0, sizeof(WellOidQueue));
    return q;
}

/* Caller holds g_sched_mutex. */
static void queue_pool_put(WellOidQueue *q) {
    if (!q) return;
    if (g_queue_pool_n >= WELL_QUEUE_POOL_MAX) {
        free(q);
        return;
    }
    memset(q, 0, sizeof(WellOidQueue));
    q->ready_next = g_queue_pool;
    g_queue_pool = q;
    g_queue_pool_n++;
}

void well_event_init(void) {
    if (g_sched_inited) return;
    well_mutex_init(&g_sched_mutex);
    well_cond_init(&g_sched_cond);
    g_sched_inited = 1;
}

void well_service_hold(void) {
    well_event_init();
    well_mutex_lock(&g_sched_mutex);
    g_service_count++;
    well_mutex_unlock(&g_sched_mutex);
}

void well_service_release(void) {
    if (!g_sched_inited) return;
    well_mutex_lock(&g_sched_mutex);
    if (g_service_count > 0) g_service_count--;
    well_cond_broadcast(&g_sched_cond);
    well_mutex_unlock(&g_sched_mutex);
}

void well_service_signal(void) {
    if (!g_sched_inited) return;
    well_mutex_lock(&g_sched_mutex);
    well_cond_broadcast(&g_sched_cond);
    well_mutex_unlock(&g_sched_mutex);
}

void well_event_shutdown(void) {
    if (!g_sched_inited) return;
    well_cond_destroy(&g_sched_cond);
    well_mutex_destroy(&g_sched_mutex);
    g_sched_inited = 0;
}

static unsigned long long qhash(long long oid) {
    unsigned long long x = (unsigned long long)oid + 0x9e3779b97f4a7c15ULL;
    x ^= x >> 30;
    x *= 0xbf58476d1ce4e5b9ULL;
    x ^= x >> 27;
    x *= 0x94d049bb133111ebULL;
    x ^= x >> 31;
    return x ? x : 1ULL;
}

static void qmap_rehash(int ncap) {
    WellOidQueue **nt = (WellOidQueue **)calloc((size_t)ncap, sizeof(WellOidQueue *));
    int i;
    if (!nt) well_panic("out of memory (oid queue map)");
    for (i = 0; i < g_qmap_cap; i++) {
        WellOidQueue *q = g_qmap[i];
        unsigned long long mask, h;
        if (!q || q == QMAP_TOMB) continue;
        mask = (unsigned long long)(ncap - 1);
        h = qhash(q->oid) & mask;
        while (nt[h]) h = (h + 1) & mask;
        nt[h] = q;
    }
    free(g_qmap);
    g_qmap = nt;
    g_qmap_cap = ncap;
    g_qmap_tomb = 0;
}

static void qmap_ensure(void) {
    if (g_qmap_cap == 0 ||
        (g_qmap_used + g_qmap_tomb + 1) * 10 >= g_qmap_cap * 7) {
        int ncap = 64;
        while (ncap * 7 < (g_qmap_used + g_qmap_tomb + 1) * 10) ncap *= 2;
        qmap_rehash(ncap);
    }
}

static int qmap_find_slot(long long oid) {
    unsigned long long mask, h;
    if (g_qmap_cap == 0) return -1;
    mask = (unsigned long long)(g_qmap_cap - 1);
    h = qhash(oid) & mask;
    for (;;) {
        WellOidQueue *q = g_qmap[h];
        if (!q) return -1;
        if (q != QMAP_TOMB && q->oid == oid) return (int)h;
        h = (h + 1) & mask;
    }
}

static WellOidQueue *qmap_get(long long oid) {
    int i = qmap_find_slot(oid);
    return (i >= 0) ? g_qmap[i] : NULL;
}

static WellOidQueue *qmap_create(long long oid) {
    WellOidQueue *q;
    unsigned long long mask, h;
    int tomb = -1;
    qmap_ensure();
    mask = (unsigned long long)(g_qmap_cap - 1);
    h = qhash(oid) & mask;
    for (;;) {
        WellOidQueue *s = g_qmap[h];
        if (!s) {
            if (tomb >= 0) h = (unsigned long long)tomb;
            break;
        }
        if (s == QMAP_TOMB && tomb < 0) tomb = (int)h;
        h = (h + 1) & mask;
    }
    q = queue_pool_take();
    if (!q) {
        q = (WellOidQueue *)calloc(1, sizeof(WellOidQueue));
        if (!q) well_panic("out of memory (oid queue)");
    }
    q->oid = oid;
    g_qmap[h] = q;
    g_qmap_used++;
    return q;
}

/* Caller holds the scheduler mutex. */
static void qmap_remove(WellOidQueue *q) {
    int i = qmap_find_slot(q->oid);
    if (i >= 0) {
        g_qmap[i] = QMAP_TOMB;
        g_qmap_used--;
        g_qmap_tomb++;
    }
    queue_pool_put(q);
    if (g_qmap_cap &&
        (g_qmap_used + g_qmap_tomb) * 10 >= g_qmap_cap * 7)
        qmap_rehash(g_qmap_cap);
}

/* Caller holds the scheduler mutex. O(1) deque operations. */
static void local_push(int w, WellOidQueue *q) {
    WellWorkerQueue *l = &g_local[w];
    q->ready_next = NULL;
    q->ready_prev = l->tail;
    if (l->tail) l->tail->ready_next = q;
    else l->head = q;
    l->tail = q;
    l->count++;
}

/* LIFO: own worker takes newest. */
static WellOidQueue *local_pop_tail(int w) {
    WellWorkerQueue *l = &g_local[w];
    WellOidQueue *q = l->tail;
    if (!q) return NULL;
    l->tail = q->ready_prev;
    if (l->tail) l->tail->ready_next = NULL;
    else l->head = NULL;
    q->ready_next = NULL;
    q->ready_prev = NULL;
    l->count--;
    return q;
}

/* Steal oldest from a victim. */
static WellOidQueue *local_pop_head(int w) {
    WellWorkerQueue *l = &g_local[w];
    WellOidQueue *q = l->head;
    if (!q) return NULL;
    l->head = q->ready_next;
    if (l->head) l->head->ready_prev = NULL;
    else l->tail = NULL;
    q->ready_next = NULL;
    q->ready_prev = NULL;
    l->count--;
    return q;
}

/* Caller holds the scheduler mutex. */
static void ready_push(WellOidQueue *q) {
    q->scheduled = 1;
    if (g_worker_count > 0) {
        int w = (int)(qhash(q->oid) % (unsigned long long)g_worker_count);
        local_push(w, q);
    } else {
        q->ready_next = NULL;
        q->ready_prev = NULL;
        if (g_inject_tail) g_inject_tail->ready_next = q;
        else g_inject_head = q;
        g_inject_tail = q;
    }
}

/* Caller holds the scheduler mutex. */
static WellOidQueue *inject_pop(void) {
    WellOidQueue *q = g_inject_head;
    if (!q) return NULL;
    g_inject_head = q->ready_next;
    if (!g_inject_head) g_inject_tail = NULL;
    q->ready_next = NULL;
    q->ready_prev = NULL;
    q->scheduled = 0;
    return q;
}

/* Caller holds the scheduler mutex. Try local, steal, then injector. */
static WellOidQueue *worker_take(int w) {
    WellOidQueue *q;
    int i;
    q = local_pop_tail(w);
    if (q) { q->scheduled = 0; return q; }
    for (i = 1; i < g_worker_count; i++) {
        int victim = (w + i) % g_worker_count;
        q = local_pop_head(victim);
        if (q) { q->scheduled = 0; return q; }
    }
    return inject_pop();
}

typedef struct EventPinList {
    WellOidPin *pins;
    int n, cap;
} EventPinList;

typedef struct EventScan {
    EventPinList *pl;
    void **seen;
    int sn, sc;
} EventScan;

static void event_pin_add(EventPinList *pl, long long oid) {
    int i;
    for (i = 0; i < pl->n; i++) {
        if (pl->pins[i].oid == oid) { pl->pins[i].count++; well_oid_pin(oid); return; }
    }
    if (pl->n == pl->cap) {
        int ncap = pl->cap ? pl->cap * 2 : 8;
        WellOidPin *np = (WellOidPin *)realloc(pl->pins, (size_t)ncap * sizeof(WellOidPin));
        if (!np) well_panic("out of memory (event pins)");
        pl->pins = np; pl->cap = ncap;
    }
    pl->pins[pl->n].oid = oid;
    pl->pins[pl->n].count = 1;
    pl->n++;
    well_oid_pin(oid);
}

static int event_seen(EventScan *s, void *p) {
    int i;
    for (i = 0; i < s->sn; i++) if (s->seen[i] == p) return 1;
    if (s->sn == s->sc) {
        int ncap = s->sc ? s->sc * 2 : 16;
        void **ns = (void **)realloc(s->seen, (size_t)ncap * sizeof(void *));
        if (!ns) well_panic("out of memory (event scan)");
        s->seen = ns; s->sc = ncap;
    }
    s->seen[s->sn++] = p;
    return 0;
}

static void event_pin_visit(void *obj, EventScan *s);

static void event_pin_field(void *field_ptr, const WellFieldDesc *f, void *user) {
    EventScan *s = (EventScan *)user;
    if (f->kind == WELL_FIELD_REF) {
        void *child = *(void **)field_ptr;
        if (child) event_pin_visit(child, s);
    } else if (f->kind == WELL_FIELD_ARRAY) {
        WellArray *a = *(WellArray **)field_ptr;
        long long i, n;
        if (f->elem_kind != WELL_FIELD_REF || !a) return;
        n = well_array_len(a);
        for (i = 0; i < n; i++) {
            WellValue v = well_array_get(a, i);
            if (v.p) event_pin_visit(v.p, s);
        }
    } else if (f->kind == WELL_FIELD_MAP) {
        WellMap *m = *(WellMap **)field_ptr;
        long long i, n;
        if (f->elem_kind != WELL_FIELD_REF || !m) return;
        n = well_map_len(m);
        for (i = 0; i < n; i++) {
            WellValue v = well_map_value_at(m, i);
            if (v.p) event_pin_visit(v.p, s);
        }
    } else if (f->kind == WELL_FIELD_LINKED) {
        WellLinked *l = *(WellLinked **)field_ptr;
        WellLinkedNode *node;
        if (f->elem_kind != WELL_FIELD_REF || !l) return;
        for (node = well_linked_first(l); node; node = well_linked_next(node)) {
            WellValue v = well_linked_value(node);
            if (v.p) event_pin_visit(v.p, s);
        }
    }
}

static void event_pin_visit(void *obj, EventScan *s) {
    WellObj *o = (WellObj *)obj;
    if (!o || event_seen(s, o)) return;
    if (o->type_id == WELL_T_STRING) return;
    if (o->flags & WELL_FLAG_IN_MODEL) event_pin_add(s->pl, o->oid);
    well_visit_fields(o, event_pin_field, s);
}

void well_event_pin_payload(void *event) {
    WellObj *o = (WellObj *)event;
    EventPinList *pl;
    EventScan s;
    if (!o) return;
    pl = (EventPinList *)o->exec_pins;
    if (!pl) {
        pl = (EventPinList *)calloc(1, sizeof(EventPinList));
        if (!pl) well_panic("out of memory (event pin list)");
        o->exec_pins = pl;
    }
    s.pl = pl; s.seen = NULL; s.sn = 0; s.sc = 0;
    event_pin_visit(o, &s);
    free(s.seen);
}

void well_event_unpin_payload(void *event) {
    WellObj *o = (WellObj *)event;
    EventPinList *pl;
    int i, k;
    if (!o || !o->exec_pins) return;
    pl = (EventPinList *)o->exec_pins;
    for (i = 0; i < pl->n; i++)
        for (k = 0; k < pl->pins[i].count; k++) well_oid_unpin(pl->pins[i].oid);
    free(pl->pins);
    free(pl);
    o->exec_pins = NULL;
}

void well_emit_event(void *event, long long channel) {
    WellEventNode *n;
    WellObj *ev = (WellObj *)event;
    WellOidQueue *q;
    if (!event) well_panic("emit(null event)");
    if (channel < 0) well_panic("emit: ALL is only for subscriptions");
    if (ev->oid == 0) ev->oid = well_id_util_get_id();
    well_event_init();
    well_event_pin_payload(event);
    well_retain(event);
    well_mutex_lock(&g_sched_mutex);
    n = node_pool_take();
    if (!n) {
        well_mutex_unlock(&g_sched_mutex);
        n = (WellEventNode *)malloc(sizeof(WellEventNode));
        if (!n) well_panic("out of memory (event queue)");
        well_mutex_lock(&g_sched_mutex);
    }
    n->channel = channel;
    n->event = event;
    n->kind = 0;
    n->task_fn = NULL;
    n->task_done = NULL;
    n->task_arg = NULL;
    n->next = NULL;
    q = qmap_get(ev->oid);
    if (!q) q = qmap_create(ev->oid);
    {
        int idle = (!q->scheduled && !q->running && q->head == NULL);
        if (q->tail) q->tail->next = n;
        else q->head = n;
        q->tail = n;
        if (idle) g_pending++;
        if (!q->scheduled && !q->running) {
            ready_push(q);
            well_cond_signal(&g_sched_cond);
        }
    }
    well_mutex_unlock(&g_sched_mutex);
}

void well_schedule_task(long long oid, WellTaskFn fn, void *arg, WellTaskDone done) {
    WellEventNode *n;
    WellOidQueue *q;
    if (!fn) well_panic("schedule_task(null)");
    if (oid == 0) oid = well_id_util_get_id();
    well_event_init();
    well_mutex_lock(&g_sched_mutex);
    n = node_pool_take();
    if (!n) {
        well_mutex_unlock(&g_sched_mutex);
        n = (WellEventNode *)malloc(sizeof(WellEventNode));
        if (!n) well_panic("out of memory (task queue)");
        well_mutex_lock(&g_sched_mutex);
    }
    n->channel = 0;
    n->event = NULL;
    n->kind = 1;
    n->task_fn = fn;
    n->task_done = done;
    n->task_arg = arg;
    n->next = NULL;
    q = qmap_get(oid);
    if (!q) q = qmap_create(oid);
    {
        int idle = (!q->scheduled && !q->running && q->head == NULL);
        if (q->tail) q->tail->next = n;
        else q->head = n;
        q->tail = n;
        if (idle) g_pending++;
        if (!q->scheduled && !q->running) {
            ready_push(q);
            well_cond_signal(&g_sched_cond);
        }
    }
    well_mutex_unlock(&g_sched_mutex);
}

static void dispatch_event(WellEventNode *node) {
    WellObj *ev = (WellObj *)node->event;
    WellSub *s;
    WellSub **matches = NULL;
    int nm = 0, cap = 0, i;

    well_model_touch_oid(ev->oid);
    well_lock_acquire(&g_subs_lock);
    for (s = g_subs; s; s = s->next) {
        if (!s->active) continue;
        if (s->event_type != ev->type_id) continue;
        if (s->channel != WELL_CHANNEL_ALL && s->channel != node->channel) continue;
        if (s->once) {
            s->active = 0; /* claim once under the lock */
            s->claimed = 1;
        }
        if (nm == cap) {
            int ncap = cap ? cap * 2 : 8;
            WellSub **nmatch = (WellSub **)realloc(matches, (size_t)ncap * sizeof(WellSub *));
            if (!nmatch) well_panic("out of memory (dispatch)");
            matches = nmatch;
            cap = ncap;
        }
        matches[nm++] = s;
    }
    well_lock_release(&g_subs_lock);

    for (i = 0; i < nm; i++) {
        WellSub *sub = matches[i];
        volatile WellCtx ctx;
        jmp_buf jb;
        jmp_buf *old;
        old = well_panic_push(&jb);
        well_ctx_init((WellCtx *)&ctx, ev->oid);
        if (setjmp(jb) == 0) {
            sub->fn((WellCtx *)&ctx, node->event);
            well_panic_pop(old);
        } else {
            /* handler panic: release the frame and continue the task loop */
            well_panic_pop(old);
            well_temp_reset();
        }
        well_ctx_clear((WellCtx *)&ctx);
    }
    free(matches);
}

static void dispatch_task(long long oid, WellEventNode *node) {
    WellCtx ctx;
    jmp_buf jb;
    jmp_buf *old;
    well_ctx_init(&ctx, oid);
    old = well_panic_push(&jb);
    if (setjmp(jb) == 0) {
        node->task_fn(&ctx, node->task_arg);
        well_panic_pop(old);
    } else {
        well_panic_pop(old);
        well_temp_reset();
    }
    well_ctx_clear(&ctx);
    if (node->task_done) node->task_done(node->task_arg);
}

/* One scheduler worker. Returns when g_stop is set (non-main) or idle (main). */
/* Caller holds the scheduler mutex. A plain signal may wake another worker,
 * but only the main worker checks the idle-exit predicate, so use a broadcast
 * when the predicate can have become true. */
static void sched_notify(void) {
    if (g_pending == 0 && g_service_count == 0)
        well_cond_broadcast(&g_sched_cond);
    else
        well_cond_signal(&g_sched_cond);
}

static void sched_worker_loop(int wid, int is_main) {
    for (;;) {
        WellOidQueue *q;
        WellEventNode *node;

        well_model_cleanup_if_due();

        well_mutex_lock(&g_sched_mutex);
        if (is_main && g_pending == 0 && g_service_count == 0) {
            well_mutex_unlock(&g_sched_mutex);
            return;
        }
        for (;;) {
            q = worker_take(wid);
            if (q) break;
            if (is_main && g_pending == 0 && g_service_count == 0) {
                well_mutex_unlock(&g_sched_mutex);
                return;
            }
            if (g_stop && !is_main) {
                well_cond_signal(&g_sched_cond);
                well_mutex_unlock(&g_sched_mutex);
                return;
            }
            well_cond_wait(&g_sched_cond, &g_sched_mutex);
            }
        q->running = 1;
        {
            node = q->head;
            q->head = node->next;
            if (!q->head) q->tail = NULL;
            well_mutex_unlock(&g_sched_mutex);

            if (node->kind == 1) dispatch_task(q->oid, node);
            else {
                dispatch_event(node);
                well_release(node->event);
            }
            }

        well_mutex_lock(&g_sched_mutex);
        node_pool_put(node);
        q->running = 0;
        if (q->head) {
            if (!q->scheduled) ready_push(q);
            sched_notify();
        } else {
            if (g_pending > 0) g_pending--;
            qmap_remove(q);
            sched_notify();
        }
        well_mutex_unlock(&g_sched_mutex);
    }
}

static void *well_worker_thread(void *arg) {
    int wid = (int)(long)arg;
    sched_worker_loop(wid, 0);
    return NULL;
}

int well_scheduler_try_begin_cleanup(long long oid) {
    WellOidQueue *q;
    well_mutex_lock(&g_sched_mutex);
    q = qmap_get(oid);
    if (q && (q->head || q->scheduled || q->running)) {
        well_mutex_unlock(&g_sched_mutex);
        return 0;
    }
    return 1;
}

void well_scheduler_end_cleanup(void) {
    well_mutex_unlock(&g_sched_mutex);
}

void well_run_loop(void) {
    int i;
    int workers = 1;
    const char *env = getenv("WELL_WORKERS");
    well_event_init();
    if (env && env[0]) {
        int v = atoi(env);
        if (v >= 1 && v <= WELL_MAX_WORKERS) workers = v;
    }

    well_mutex_lock(&g_sched_mutex);
    if (g_pending == 0 && g_service_count == 0) {
        well_mutex_unlock(&g_sched_mutex);
        return;
    }
    g_stop = 0;
    g_thread_count = workers - 1;
    if (g_thread_count > WELL_MAX_WORKERS) g_thread_count = WELL_MAX_WORKERS;
    g_worker_count = workers;
    for (i = 0; i < g_thread_count; i++) {
        if (well_thread_start(&g_threads[i], well_worker_thread, (void *)(long)(i + 1)) != 0)
            well_panic("cannot start worker thread");
    }
    well_mutex_unlock(&g_sched_mutex);

    sched_worker_loop(0, 1);

    well_mutex_lock(&g_sched_mutex);
    g_stop = 1;
    well_cond_broadcast(&g_sched_cond);
    well_mutex_unlock(&g_sched_mutex);

    for (i = 0; i < g_thread_count; i++)
        well_thread_join(g_threads[i]);
    g_thread_count = 0;
    g_worker_count = 0;
    memset(g_local, 0, sizeof(g_local));
}

