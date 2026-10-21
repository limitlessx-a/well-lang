#include "wellrt.h"
#include "well_internal.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdarg.h>
#include <setjmp.h>
#include <time.h>

#ifndef _WIN32
#include <sys/time.h>
#endif

#ifdef _WIN32
#include <windows.h>
#endif

#define WELL_MAX_TYPES 4096

static void (*g_destroy[WELL_MAX_TYPES])(void *);
static const WellTypeDesc *g_type_desc[WELL_MAX_TYPES];
static int g_inited = 0;
static long long g_live = 0;

/* ---------- oid object registry ---------- */
#define REG_SHARDS 256
typedef struct { WellLock lock; WellObj *head; } WellRegShard;
static WellRegShard g_reg[REG_SHARDS];
static unsigned reg_idx(long long oid) {
    unsigned long long x = (unsigned long long)oid;
    x ^= x >> 33; x *= 0xff51afd7ed558ccdULL; x ^= x >> 33;
    return (unsigned)(x & (REG_SHARDS - 1));
}
static void well_registry_add(WellObj *o) {
    unsigned i;
    if (!o) return;
    i = reg_idx(o->oid);
    well_lock_acquire(&g_reg[i].lock);
    o->reg_prev = NULL;
    o->reg_next = g_reg[i].head;
    if (g_reg[i].head) g_reg[i].head->reg_prev = o;
    g_reg[i].head = o;
    well_lock_release(&g_reg[i].lock);
}
static void well_registry_remove(WellObj *o) {
    unsigned i;
    if (!o) return;
    i = reg_idx(o->oid);
    well_lock_acquire(&g_reg[i].lock);
    if (o->reg_prev) o->reg_prev->reg_next = o->reg_next;
    else if (g_reg[i].head == o) g_reg[i].head = o->reg_next;
    if (o->reg_next) o->reg_next->reg_prev = o->reg_prev;
    o->reg_next = NULL; o->reg_prev = NULL;
    well_lock_release(&g_reg[i].lock);
}
static WellObj *well_registry_take(long long oid) {
    unsigned i = reg_idx(oid);
    WellObj *p, *next, *keep = NULL, *out = NULL;
    well_lock_acquire(&g_reg[i].lock);
    p = g_reg[i].head;
    while (p) {
        next = p->reg_next;
        p->reg_prev = NULL;
        if (p->oid == oid) {
            p->reg_next = out;
            out = p;
        } else {
            p->reg_next = keep;
            if (keep) keep->reg_prev = p;
            keep = p;
        }
        p = next;
    }
    g_reg[i].head = keep;
    well_lock_release(&g_reg[i].lock);
    return out;
}

/* ---------- fixed-size object pool ---------- */
#define POOL_MAX_SIZE 4096u
#define POOL_ALIGN 16u
#define POOL_CLASSES (POOL_MAX_SIZE / POOL_ALIGN + 1u)
#define POOL_DEFAULT_TTL_MS 60000LL
#define POOL_DEFAULT_MAX_MB 64LL

typedef struct PoolNode {
    struct PoolNode *next;
    size_t size;
    long long inserted_ms;
} PoolNode;

static PoolNode *g_pool[POOL_CLASSES];
static size_t g_pool_bytes = 0;
static WellLock g_pool_lock = 0;

static size_t pool_class(size_t size) {
    return (size + POOL_ALIGN - 1u) / POOL_ALIGN;
}

static size_t pool_limit_bytes(void);
static void *pool_take(size_t size) {
    size_t c = pool_class(size);
    PoolNode *n;
    if (size > POOL_MAX_SIZE) return NULL;
    well_lock_acquire(&g_pool_lock);
    n = g_pool[c];
    if (n) {
        g_pool[c] = n->next;
        g_pool_bytes -= n->size;
    }
    well_lock_release(&g_pool_lock);
    return n;
}

void well_obj_free(void *obj) {
    WellObj *o = (WellObj *)obj;
    PoolNode *n;
    size_t size, c;
    if (!o) return;
    size = o->alloc_size;
    if (size == 0 || size > POOL_MAX_SIZE) { free(o); return; }
    c = pool_class(size);
    n = (PoolNode *)o;
    well_lock_acquire(&g_pool_lock);
    n->size = c * POOL_ALIGN;
    if (g_pool_bytes + n->size > pool_limit_bytes()) {
        well_lock_release(&g_pool_lock);
        free(o);
        return;
    }
    n->inserted_ms = well_mono_ms();
    n->next = g_pool[c];
    g_pool[c] = n;
    g_pool_bytes += n->size;
    well_lock_release(&g_pool_lock);
}

void well_pool_sweep(void) {
    const char *e = getenv("WELL_POOL_TTL_MS");
    long long ttl = e && e[0] ? (long long)atoi(e) : POOL_DEFAULT_TTL_MS;
    long long now = well_mono_ms();
    int i;
    if (ttl <= 0) return;
    well_lock_acquire(&g_pool_lock);
    for (i = 1; i < (int)POOL_CLASSES; i++) {
        PoolNode **pp = &g_pool[i];
        while (*pp) {
            PoolNode *n = *pp;
            if (now - n->inserted_ms >= ttl) {
                *pp = n->next;
                g_pool_bytes -= n->size;
                free(n);
            } else {
                pp = &n->next;
            }
        }
    }
    well_lock_release(&g_pool_lock);
}

static size_t pool_limit_bytes(void) {
    const char *e = getenv("WELL_POOL_MAX_MB");
    long long mb = e && e[0] ? atoll(e) : POOL_DEFAULT_MAX_MB;
    if (mb <= 0) return 0;
    return (size_t)mb * 1024u * 1024u;
}

static void well_pool_shutdown(void) {
    int i;
    well_lock_acquire(&g_pool_lock);
    for (i = 1; i < (int)POOL_CLASSES; i++) {
        PoolNode *n = g_pool[i];
        while (n) {
            PoolNode *next = n->next;
            free(n);
            n = next;
        }
        g_pool[i] = NULL;
    }
    g_pool_bytes = 0;
    well_lock_release(&g_pool_lock);
}
#ifdef _WIN32
/* TCC's __declspec(thread) is not per-thread for CreateThread; use Win32 TLS. */
static DWORD g_panic_tls = TLS_OUT_OF_INDEXES;
static jmp_buf *panic_env_get(void) {
    return (g_panic_tls == TLS_OUT_OF_INDEXES) ? NULL : (jmp_buf *)TlsGetValue(g_panic_tls);
}
static void panic_env_set(jmp_buf *p) {
    if (g_panic_tls != TLS_OUT_OF_INDEXES) TlsSetValue(g_panic_tls, p);
}
#else
static WELL_THREAD_LOCAL jmp_buf *g_panic_env = NULL;
static jmp_buf *panic_env_get(void) { return g_panic_env; }
static void panic_env_set(jmp_buf *p) { g_panic_env = p; }
#endif
static WellMutex g_print_mutex;
static int g_print_inited = 0;

/* ---------- atomic RC ---------- */
#ifdef _WIN32
static long well_atomic_load(WellAtomicInt *p) {
    return InterlockedCompareExchange((volatile LONG *)p, 0, 0);
}
static long well_atomic_inc(WellAtomicInt *p) {
    return InterlockedIncrement((volatile LONG *)p);
}
static long well_atomic_dec(WellAtomicInt *p) {
    return InterlockedDecrement((volatile LONG *)p);
}
#elif defined(__GNUC__) || defined(__clang__)
static long well_atomic_load(WellAtomicInt *p) {
    return __atomic_load_n(p, __ATOMIC_RELAXED);
}
static long well_atomic_inc(WellAtomicInt *p) {
    return __atomic_add_fetch(p, 1, __ATOMIC_RELAXED);
}
static long well_atomic_dec(WellAtomicInt *p) {
    return __atomic_sub_fetch(p, 1, __ATOMIC_ACQ_REL);
}
#else
/* Single-thread fallback; no atomic support on this compiler. */
static long well_atomic_load(WellAtomicInt *p) { return *p; }
static long well_atomic_inc(WellAtomicInt *p) { return ++(*p); }
static long well_atomic_dec(WellAtomicInt *p) { return --(*p); }
#endif

static void string_destroy(void *p);

/* ---------- spinlock ---------- */
void well_lock_acquire(WellLock *l) {
#ifdef _WIN32
    while (InterlockedCompareExchange((volatile LONG *)l, 1, 0) != 0)
        Sleep(0);
#elif defined(__GNUC__) || defined(__clang__)
    while (__atomic_exchange_n(l, 1, __ATOMIC_ACQUIRE)) {
        while (__atomic_load_n(l, __ATOMIC_RELAXED)) { }
    }
#else
    while (*l) { }
    *l = 1;
#endif
}

void well_lock_release(WellLock *l) {
#ifdef _WIN32
    InterlockedExchange((volatile LONG *)l, 0);
#elif defined(__GNUC__) || defined(__clang__)
    __atomic_store_n(l, 0, __ATOMIC_RELEASE);
#else
    *l = 0;
#endif
}

/* ---------- helpers ---------- */
void well_panic(const char *msg) {
    jmp_buf *env = panic_env_get();
    fprintf(stderr, "well panic: %s\n", msg);
    fflush(stderr);
    if (env) longjmp(*env, 1);
    exit(1);
}

jmp_buf *well_panic_push(jmp_buf *env) {
    jmp_buf *old = panic_env_get();
    panic_env_set(env);
    return old;
}

void well_panic_pop(jmp_buf *old) {
    panic_env_set(old);
}

/* ---------- object graph (DAG enforcement) ---------- */
typedef struct WellEdge {
    WellObj *to;
    int count;
    struct WellEdge *next;
} WellEdge;

static WellLock g_graph_lock = 0;

static int graph_seen(WellObj **seen, int n, WellObj *o) {
    int i;
    for (i = 0; i < n; i++) if (seen[i] == o) return 1;
    return 0;
}

static int graph_reaches_rec(WellObj *cur, WellObj *target,
                             WellObj ***seen, int *n, int *cap) {
    WellEdge *e;
    if (cur == target) return 1;
    if (graph_seen(*seen, *n, cur)) return 0;
    if (*n == *cap) {
        int ncap = *cap ? *cap * 2 : 16;
        WellObj **ns = (WellObj **)realloc(*seen, (size_t)ncap * sizeof(WellObj *));
        if (!ns) well_panic("out of memory (DAG)");
        *seen = ns;
        *cap = ncap;
    }
    (*seen)[(*n)++] = cur;
    for (e = (WellEdge *)cur->graph_edges; e; e = e->next) {
        if (graph_reaches_rec(e->to, target, seen, n, cap)) return 1;
    }
    return 0;
}

static int graph_reaches(WellObj *start, WellObj *target) {
    WellObj **seen = NULL;
    int n = 0, cap = 0;
    int r = graph_reaches_rec(start, target, &seen, &n, &cap);
    free(seen);
    return r;
}

static void well_graph_clear_edges(WellObj *o) {
    WellEdge *e, *next;
    if (!o) return;
    well_lock_acquire(&g_graph_lock);
    e = (WellEdge *)o->graph_edges;
    o->graph_edges = NULL;
    well_lock_release(&g_graph_lock);
    while (e) {
        next = e->next;
        free(e);
        e = next;
    }
}

void well_graph_add_edge(WellObj *from, WellObj *to) {
    (void)from;
    (void)to;
}

void well_graph_remove_edge(WellObj *from, WellObj *to) {
    WellEdge **pp, *e;
    if (!from || !to || to->type_id == WELL_T_STRING) return;
    well_lock_acquire(&g_graph_lock);
    pp = (WellEdge **)&from->graph_edges;
    while (*pp) {
        e = *pp;
        if (e->to == to) {
            if (--e->count <= 0) {
                *pp = e->next;
                free(e);
            }
            well_lock_release(&g_graph_lock);
            return;
        }
        pp = &e->next;
    }
    well_lock_release(&g_graph_lock);
}

void well_init(void) {
    memset(g_destroy, 0, sizeof(g_destroy));
    memset(g_type_desc, 0, sizeof(g_type_desc));
    g_inited = 1;
#ifdef _WIN32
    if (g_panic_tls == TLS_OUT_OF_INDEXES)
        g_panic_tls = TlsAlloc();
#endif
    well_mutex_init(&g_print_mutex);
    g_print_inited = 1;
    well_register_type(WELL_T_STRING, string_destroy);
    well_containers_register();
    well_event_init();
    well_model_init();
}

/* ---------- object / rc ---------- */
void *well_new(size_t size, unsigned int type_id, long long oid) {
    WellObj *o = (WellObj *)pool_take(size);
    if (o) memset(o, 0, size);
    else o = (WellObj *)calloc(1, size);
    if (!o) well_panic("out of memory (well_new)");
    o->rc = 1;
    o->type_id = type_id;
    o->oid = oid;
    o->flags = 0;
    o->graph_edges = NULL;
    o->exec_pins = NULL;
    o->reg_next = NULL;
    o->reg_prev = NULL;
    o->alloc_size = size;
    g_live++;
    well_registry_add(o);
    return o;
}

void well_register_type(unsigned int id, void (*destroy)(void *)) {
    if (id < WELL_MAX_TYPES) g_destroy[id] = destroy;
}

void well_register_type_desc(const WellTypeDesc *desc) {
    if (desc && desc->type_id < WELL_MAX_TYPES)
        g_type_desc[desc->type_id] = desc;
}

const WellTypeDesc *well_type_desc(unsigned int type_id) {
    return type_id < WELL_MAX_TYPES ? g_type_desc[type_id] : NULL;
}

const WellFieldDesc *well_type_field(const WellTypeDesc *desc, const char *name) {
    int i;
    if (!desc || !name) return NULL;
    for (i = 0; i < desc->field_count; i++) {
        if (strcmp(desc->fields[i].name, name) == 0)
            return &desc->fields[i];
    }
    return NULL;
}

void well_visit_fields(void *obj, WellFieldVisitor fn, void *user) {
    WellObj *o = (WellObj *)obj;
    const WellTypeDesc *d;
    int i;
    if (!o || !fn) return;
    d = well_type_desc(o->type_id);
    if (!d) return;
    for (i = 0; i < d->field_count; i++) {
        const WellFieldDesc *f = &d->fields[i];
        fn((char *)obj + f->offset, f, user);
    }
}

static const char *well_field_kind_name(int kind) {
    switch (kind) {
    case WELL_FIELD_INT: return "int";
    case WELL_FIELD_LONG: return "long";
    case WELL_FIELD_DOUBLE: return "double";
    case WELL_FIELD_BOOL: return "bool";
    case WELL_FIELD_STRING: return "String";
    case WELL_FIELD_REF: return "ref";
    case WELL_FIELD_ARRAY: return "Array";
    case WELL_FIELD_MAP: return "Map";
    case WELL_FIELD_LINKED: return "Linked";
    default: return "?";
    }
}

static void well_meta_dump_field(void *p, const WellFieldDesc *f, void *user) {
    (void)p;
    (void)user;
    fprintf(stderr, "  %s kind=%s type=%u elem=%s/%u\n",
            f->name, well_field_kind_name(f->kind), f->type_id,
            well_field_kind_name(f->elem_kind), f->elem_type_id);
}

void well_type_debug_dump(void) {
    unsigned int i;
    for (i = 0; i < WELL_MAX_TYPES; i++) {
        const WellTypeDesc *d = g_type_desc[i];
        if (!d) continue;
        fprintf(stderr, "well type %u %s event=%d\n",
                d->type_id, d->name, d->is_event);
        {
            int k;
            for (k = 0; k < d->field_count; k++)
                well_meta_dump_field(NULL, &d->fields[k], NULL);
        }
    }
}

void well_retain(void *obj) {
    WellObj *o = (WellObj *)obj;
    if (!o) return;
    if (o->flags & WELL_FLAG_IMMORTAL) return;
    if (well_atomic_load(&o->rc) <= 0) well_panic("retain of dead object");
    well_atomic_inc(&o->rc);
}

void well_release(void *obj) {
    WellObj *o = (WellObj *)obj;
    if (!o) return;
    if (o->flags & WELL_FLAG_IMMORTAL) return;
    if (well_atomic_load(&o->rc) <= 0) well_panic("release of dead object");
    if (well_atomic_dec(&o->rc) == 0) {
        g_live--;
        well_graph_clear_edges(o);
        well_event_unpin_payload(o);
        well_registry_remove(o);
        if (o->type_id < WELL_MAX_TYPES && g_destroy[o->type_id])
            g_destroy[o->type_id](o);
        else
            free(o);
    }
}

/* ---------- frame journal ---------- */
void well_ctx_init(WellCtx *ctx, long long oid) {
    ctx->oid = oid;
    ctx->refs = NULL;
    ctx->n = 0;
    ctx->cap = 0;
    ctx->pins = NULL;
    ctx->pin_n = 0;
    ctx->pin_cap = 0;
}

static void ctx_push(WellCtx *ctx, void *obj) {
    if (ctx->n == ctx->cap) {
        int ncap = ctx->cap ? ctx->cap * 2 : 8;
        void **nr = (void **)realloc(ctx->refs, (size_t)ncap * sizeof(void *));
        if (!nr) well_panic("out of memory (ctx refs)");
        ctx->refs = nr;
        ctx->cap = ncap;
    }
    ctx->refs[ctx->n++] = obj;
}

void well_track(WellCtx *ctx, void *obj) {
    if (!obj) return;
    well_retain(obj);
    ctx_push(ctx, obj);
}

void well_track_owned(WellCtx *ctx, void *obj) {
    if (!obj) return;
    ctx_push(ctx, obj);
}

void well_ctx_pin(WellCtx *ctx, long long oid) {
    int i;
    if (!ctx) return;
    for (i = 0; i < ctx->pin_n; i++) {
        if (ctx->pins[i].oid == oid) {
            ctx->pins[i].count++;
            well_oid_pin(oid);
            return;
        }
    }
    if (ctx->pin_n == ctx->pin_cap) {
        int ncap = ctx->pin_cap ? ctx->pin_cap * 2 : 8;
        WellOidPin *np = (WellOidPin *)realloc(ctx->pins, (size_t)ncap * sizeof(WellOidPin));
        if (!np) well_panic("out of memory (ctx pins)");
        ctx->pins = np;
        ctx->pin_cap = ncap;
    }
    ctx->pins[ctx->pin_n].oid = oid;
    ctx->pins[ctx->pin_n].count = 1;
    ctx->pin_n++;
    well_oid_pin(oid);
}

void well_ctx_unpin(WellCtx *ctx, long long oid) {
    int i;
    if (!ctx) return;
    for (i = 0; i < ctx->pin_n; i++) {
        if (ctx->pins[i].oid == oid) {
            ctx->pins[i].count--;
            well_oid_unpin(oid);
            if (ctx->pins[i].count <= 0) {
                ctx->pins[i] = ctx->pins[ctx->pin_n - 1];
                ctx->pin_n--;
            }
            return;
        }
    }
}

void well_ctx_replace(WellCtx *ctx, void **slot, void *new_value, int owned) {
    void *old;
    int i, found = -1;
    if (!ctx || !slot) return;
    old = *slot;
    if (new_value && !owned) well_retain(new_value);
    if (old) {
        for (i = 0; i < ctx->n; i++) {
            if (ctx->refs[i] == old) { found = i; break; }
        }
    }
    if (found >= 0) {
        ctx->refs[found] = new_value;
        if (!(old == new_value && owned)) well_release(old);
    } else if (new_value) {
        if (owned) well_track_owned(ctx, new_value);
        else well_track_owned(ctx, new_value); /* already retained above */
    }
    *slot = new_value;
}

int well_ctx_mark(WellCtx *ctx) {
    return ctx ? ctx->n : 0;
}

void well_ctx_release_from(WellCtx *ctx, int mark) {
    int i;
    if (!ctx) return;
    if (mark < 0) mark = 0;
    if (mark > ctx->n) mark = ctx->n;
    for (i = ctx->n - 1; i >= mark; i--) well_release(ctx->refs[i]);
    ctx->n = mark;
}

void well_ctx_clear(WellCtx *ctx) {
    int i;
    for (i = 0; i < ctx->n; i++) well_release(ctx->refs[i]);
    free(ctx->refs);
    ctx->refs = NULL;
    ctx->n = 0;
    ctx->cap = 0;
    for (i = 0; i < ctx->pin_n; i++) {
        int k;
        for (k = 0; k < ctx->pins[i].count; k++) well_oid_unpin(ctx->pins[i].oid);
    }
    free(ctx->pins);
    ctx->pins = NULL;
    ctx->pin_n = 0;
    ctx->pin_cap = 0;
}

/* ---------- ownership ---------- */
void well_check_oid(void *obj, WellCtx *ctx, const char *where) {
    WellObj *o = (WellObj *)obj;
    if (!o) well_panic("null object write");
    if (o->flags & WELL_FLAG_FROZEN) well_panic("write to frozen event");
    if (o->oid != ctx->oid) {
        char msg[256];
        snprintf(msg, sizeof(msg),
                 "well oid violation: %s (object oid=%lld, handler oid=%lld)",
                 where ? where : "write", o->oid, ctx->oid);
        well_panic(msg);
    }
}

void well_freeze(void *obj) {
    WellObj *o = (WellObj *)obj;
    if (o) o->flags |= WELL_FLAG_FROZEN;
}

void well_set_ref(void *owner, void **slot, void *val) {
    void *old = *slot;
    if (val) well_graph_add_edge((WellObj *)owner, (WellObj *)val);
    well_retain(val);
    *slot = val;
    if (old) {
        well_graph_remove_edge((WellObj *)owner, (WellObj *)old);
        well_release(old);
    }
}

void well_check_value_oid(void *target, void *value, WellCtx *ctx) {
    WellObj *t = (WellObj *)target;
    WellObj *v = (WellObj *)value;
    (void)ctx;
    if (!v) return;
    if (v->type_id == WELL_T_STRING) return; /* String has no oid */
    if (!t) well_panic("null field target");
    if (t->oid != v->oid) {
        char msg[256];
        snprintf(msg, sizeof(msg),
                 "well oid violation: field value (target oid=%lld, value oid=%lld)",
                 t->oid, v->oid);
        well_panic(msg);
    }
}

/* ---------- Strings ---------- */
typedef struct {
    WellString **items;
    int n;
    int cap;
} WellLitTable;
static WellLitTable g_lits = {NULL, 0, 0};

/* FNV-1a 64-bit (public domain); strings are immutable so hash is cached. */
static unsigned long long hash_bytes(const char *s, size_t n) {
    unsigned long long h = 1469598103934665603ULL;
    size_t i;
    for (i = 0; i < n; i++) {
        h ^= (unsigned char)s[i];
        h *= 1099511628211ULL;
    }
    return h ? h : 1ULL;
}

static void string_set_hash(WellString *s) {
    s->hash = hash_bytes(s->data, strlen(s->data));
}

static void string_destroy(void *p) {
    WellString *s = (WellString *)p;
    free(s->data);
    free(s);
}

static WellString *string_alloc_n(const char *src, size_t n) {
    WellString *s = (WellString *)malloc(sizeof(WellString));
    if (!s) well_panic("out of memory (String)");
    s->data = (char *)malloc(n + 1);
    if (!s->data) well_panic("out of memory (String data)");
    if (n && src) memcpy(s->data, src, n);
    s->data[n] = 0;
    s->hdr.rc = 1;
    s->hdr.type_id = WELL_T_STRING;
    s->hdr.flags = 0;
    s->hdr.oid = 0;
    s->hdr.graph_edges = NULL;
    s->hdr.exec_pins = NULL;
    s->hdr.reg_next = NULL;
    s->hdr.reg_prev = NULL;
    s->hdr.alloc_size = sizeof(WellString);
    s->hash = hash_bytes(s->data, n);
    g_live++;
    return s;
}

static WellString *string_alloc(const char *src) {
    return string_alloc_n(src, strlen(src ? src : ""));
}

WellString *well_string_new(const char *src) {
    return string_alloc(src);
}

WellString *well_string_lit(const char *src) {
    int i;
    WellString *s;
    if (!g_inited) well_init();
    for (i = 0; i < g_lits.n; i++) {
        if (strcmp(g_lits.items[i]->data, src ? src : "") == 0)
            return g_lits.items[i];
    }
    s = string_alloc(src);
    s->hdr.flags |= WELL_FLAG_IMMORTAL;
    g_live--; /* literals are process-lifetime and not "live objects" */
    if (g_lits.n == g_lits.cap) {
        int ncap = g_lits.cap ? g_lits.cap * 2 : 32;
        WellString **ni = (WellString **)realloc(g_lits.items, (size_t)ncap * sizeof(WellString *));
        if (!ni) well_panic("out of memory (literals)");
        g_lits.items = ni;
        g_lits.cap = ncap;
    }
    g_lits.items[g_lits.n++] = s;
    return s;
}

WellString *well_string_concat(WellString *a, WellString *b) {
    const char *sa = a ? a->data : "";
    const char *sb = b ? b->data : "";
    size_t la = strlen(sa), lb = strlen(sb);
    WellString *s = (WellString *)malloc(sizeof(WellString));
    if (!s) well_panic("out of memory (String)");
    s->data = (char *)malloc(la + lb + 1);
    if (!s->data) well_panic("out of memory (String data)");
    memcpy(s->data, sa, la);
    memcpy(s->data + la, sb, lb);
    s->data[la + lb] = 0;
    s->hdr.rc = 1;
    s->hdr.type_id = WELL_T_STRING;
    s->hdr.flags = 0;
    s->hdr.oid = 0;
    s->hdr.graph_edges = NULL;
    s->hdr.exec_pins = NULL;
    s->hdr.reg_next = NULL;
    s->hdr.reg_prev = NULL;
    s->hdr.alloc_size = sizeof(WellString);
    string_set_hash(s);
    g_live++;
    return s;
}

const char *well_string_cstr(WellString *s) {
    return s ? s->data : "";
}

int well_string_eq(WellString *a, WellString *b) {
    if (a == b) return 1;
    if (!a || !b) return 0;
    if (a->hash != b->hash) return 0;
    return strcmp(a->data, b->data) == 0;
}

long long well_string_len(WellString *s) {
    return (long long)strlen(s ? s->data : "");
}

/* ---------- String helpers (byte oriented, matching String.length) ---------- */

long long well_str_byte_at(WellString *s, long long i) {
    long long n = well_string_len(s);
    if (!s || i < 0 || i >= n) return -1;
    return (long long)(unsigned char)s->data[i];
}

WellString *well_str_from_byte(int code) {
    char c = (char)(unsigned char)(code & 0xFF);
    return string_alloc_n(&c, 1);
}

WellString *well_str_substring(WellString *s, long long start, long long end) {
    const char *d = s ? s->data : "";
    long long n = well_string_len(s);
    if (start < 0) start = 0;
    if (end > n) end = n;
    if (start > end) start = end;
    return string_alloc_n(d + start, (size_t)(end - start));
}

long long well_str_index_of_from(WellString *s, WellString *needle, long long from) {
    const char *d = s ? s->data : "";
    const char *hit;
    long long n = well_string_len(s);
    if (from < 0) from = 0;
    if (from > n) return -1;
    if (!needle || needle->data[0] == 0) return from;
    hit = strstr(d + from, needle->data);
    return hit ? (long long)(hit - d) : -1;
}

long long well_str_index_of(WellString *s, WellString *needle) {
    return well_str_index_of_from(s, needle, 0);
}

int well_str_contains(WellString *s, WellString *needle) {
    return well_str_index_of(s, needle) >= 0;
}

int well_str_starts_with(WellString *s, WellString *prefix) {
    const char *d = s ? s->data : "";
    const char *p = prefix ? prefix->data : "";
    size_t pn = strlen(p);
    if (strlen(d) < pn) return 0;
    return memcmp(d, p, pn) == 0;
}

int well_str_ends_with(WellString *s, WellString *suffix) {
    const char *d = s ? s->data : "";
    const char *p = suffix ? suffix->data : "";
    size_t dn = strlen(d), pn = strlen(p);
    if (dn < pn) return 0;
    return memcmp(d + dn - pn, p, pn) == 0;
}

static int str_is_space(char c) {
    return c == ' ' || c == '\t' || c == '\r' || c == '\n';
}

WellString *well_str_trim(WellString *s) {
    const char *d = s ? s->data : "";
    size_t a = 0, b = strlen(d);
    while (a < b && str_is_space(d[a])) a++;
    while (b > a && str_is_space(d[b - 1])) b--;
    return string_alloc_n(d + a, b - a);
}

WellArray *well_str_split(WellString *s, WellString *sep, WellCtx *ctx) {
    WellArray *a = well_array_new(ctx, 1);
    const char *d = s ? s->data : "";
    const char *sp = sep ? sep->data : "";
    size_t spl = strlen(sp);
    WellValue v;
    const char *p = d;
    if (spl == 0) {
        v.p = well_string_new(d);
        well_array_push(a, v, ctx);
        well_release(v.p);
        return a;
    }
    for (;;) {
        const char *hit = strstr(p, sp);
        size_t n = hit ? (size_t)(hit - p) : strlen(p);
        WellString *part = string_alloc_n(p, n);
        v.p = part;
        well_array_push(a, v, ctx);
        well_release(part);
        if (!hit) break;
        p = hit + spl;
    }
    return a;
}



void well_shutdown(void) {
    if (getenv("WELL_TYPE_DEBUG")) well_type_debug_dump();
    well_module_shutdown_all();
    well_model_shutdown();
    well_event_shutdown();
    well_pool_shutdown();
    if (g_print_inited) {
        well_mutex_destroy(&g_print_mutex);
        g_print_inited = 0;
    }
#ifdef _WIN32
    if (g_panic_tls != TLS_OUT_OF_INDEXES) {
        TlsFree(g_panic_tls);
        g_panic_tls = TLS_OUT_OF_INDEXES;
    }
#endif
    {
        const char *dbg = getenv("WELL_RC_DEBUG");
        if (dbg && dbg[0] && dbg[0] != '0') {
            fprintf(stderr, "well: live objects at shutdown = %lld\n", g_live);
            fflush(stderr);
        }
    }
}

/* ---------- IdUtil ---------- */
#define WELL_ID_EPOCH_MS 1704067200000LL /* 2024-01-01T00:00:00Z */
#define WELL_ID_TIME_SHIFT 22
#define WELL_ID_SERVER_SHIFT 12
#define WELL_ID_SEQ_MASK 0xFFF
#define WELL_ID_SERVER_MAX 1023

static WellLock g_id_lock = 0;
static long long g_id_last_ts = 0;
static int g_id_seq = 0;
static int g_id_server = -1;

long long well_now_ms(void) {
#ifdef _WIN32
    FILETIME ft;
    ULARGE_INTEGER u;
    GetSystemTimeAsFileTime(&ft);
    u.LowPart = ft.dwLowDateTime;
    u.HighPart = ft.dwHighDateTime;
    return (long long)(u.QuadPart / 10000ULL) - 11644473600000LL;
#else
    struct timeval tv;
    gettimeofday(&tv, NULL);
    return (long long)tv.tv_sec * 1000LL + (long long)tv.tv_usec / 1000LL;
#endif
}

long long well_mono_ms(void) {
#ifdef _WIN32
    return (long long)GetTickCount();
#else
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (long long)ts.tv_sec * 1000LL + (long long)ts.tv_nsec / 1000000LL;
#endif
}

int well_rc_is_one(void *obj) {
    WellObj *o = (WellObj *)obj;
    return o && well_atomic_load(&o->rc) == 1;
}

long long well_id_util_get_id(void) {
    long long ts, id;
    well_lock_acquire(&g_id_lock);
    if (g_id_server < 0) {
        const char *env = getenv("WELL_SERVER_ID");
        g_id_server = env && env[0] ? atoi(env) : 0;
        if (g_id_server < 0 || g_id_server > WELL_ID_SERVER_MAX) {
            well_lock_release(&g_id_lock);
            well_panic("WELL_SERVER_ID out of range (0..1023)");
        }
    }
    ts = well_now_ms();
    if (ts < WELL_ID_EPOCH_MS) ts = WELL_ID_EPOCH_MS;
    if (ts > g_id_last_ts) {
        g_id_last_ts = ts;
        g_id_seq = 0;
    } else {
        g_id_seq++;
        if (g_id_seq > WELL_ID_SEQ_MASK) {
            g_id_last_ts++;
            g_id_seq = 0;
        }
    }
    id = ((g_id_last_ts - WELL_ID_EPOCH_MS) << WELL_ID_TIME_SHIFT) |
         ((long long)g_id_server << WELL_ID_SERVER_SHIFT) |
         (long long)g_id_seq;
    well_lock_release(&g_id_lock);
    return id;
}

/* ---------- print ---------- */
void well_print_begin(void) {
    if (g_print_inited) well_mutex_lock(&g_print_mutex);
}

void well_print_end(void) {
    if (g_print_inited) well_mutex_unlock(&g_print_mutex);
}

void well_print_str(WellString *s) {
    fputs(well_string_cstr(s), stdout);
}

void well_print_int(long long v) {
    printf("%lld", v);
}

void well_print_bool(int v) {
    fputs(v ? "true" : "false", stdout);
}

void well_print_double(double v) {
    printf("%g", v);
}

void well_print_nl(void) {
    fputc('\n', stdout);
    fflush(stdout);
}

long long well_live_count(void) {
    return g_live;
}

static void force_clear_field(void *field_ptr, const WellFieldDesc *f, void *user) {
    (void)user;
    if (f->kind == WELL_FIELD_STRING) {
        WellString *s = *(WellString **)field_ptr;
        if (s) well_release(s);
        *(void **)field_ptr = NULL;
    } else if (f->kind == WELL_FIELD_REF ||
               f->kind == WELL_FIELD_ARRAY ||
               f->kind == WELL_FIELD_MAP ||
               f->kind == WELL_FIELD_LINKED) {
        *(void **)field_ptr = NULL;
    }
}

static void force_free_obj(WellObj *o) {
    if (!o) return;
    if (o->type_id == WELL_T_ARRAY) {
        well_array_force_clear(o);
    } else if (o->type_id == WELL_T_MAP) {
        well_map_force_clear(o);
    } else if (o->type_id == WELL_T_LINKED) {
        well_linked_force_clear(o);
    } else if (o->type_id != WELL_T_STRING) {
        well_event_unpin_payload(o);
        well_visit_fields(o, force_clear_field, NULL);
    }
    g_live--;
    well_obj_free(o);
}

void well_force_cleanup_oid(long long oid) {
    WellObj *p = well_registry_take(oid);
    while (p) {
        WellObj *next = p->reg_next;
        force_free_obj(p);
        p = next;
    }
}
