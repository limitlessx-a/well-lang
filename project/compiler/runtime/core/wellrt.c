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

/* Live-object counter (R-07): diagnostic only (WELL_RC_DEBUG / well_live_count),
 * but it is incremented/decremented from every worker thread, so the update must
 * be atomic: a plain ++/-- is a read-modify-write data race that loses updates
 * (e.g. 8 threads creating/releasing strings left a bogus non-zero count).
 * Windows uses the 64-bit Interlocked API (TinyCC can call it too); gcc/clang
 * use __atomic; other compilers get a single-thread fallback, matching the
 * existing RC-atomic policy above. */
#if defined(_WIN32)
static volatile LONG64 g_live = 0;
#elif defined(__GNUC__) || defined(__clang__)
static volatile long long g_live = 0;
#else
static volatile long long g_live = 0;
#endif
/* Diagnostic only: the counter is off unless WELL_RC_DEBUG is set, so normal
 * runs do not pay two global atomic RMWs per object allocation/release. */
static int g_live_track = 0;
static int g_live_cfg = 0;
static void live_cfg_init(void) {
    const char *e;
    if (g_live_cfg) return;
    g_live_cfg = 1;
    e = getenv("WELL_RC_DEBUG");
    g_live_track = e && e[0] && e[0] != '0';
}
#if defined(_WIN32)
static long long live_inc(void) {
    if (!g_live_track) return 0;
    return (long long)InterlockedIncrement64(&g_live);
}
static long long live_dec(void) {
    if (!g_live_track) return 0;
    return (long long)InterlockedDecrement64(&g_live);
}
static long long live_load(void) {
    if (!g_live_track) return 0;
    return (long long)InterlockedCompareExchange64(&g_live, 0, 0);
}
#elif defined(__GNUC__) || defined(__clang__)
static long long live_inc(void) {
    if (!g_live_track) return 0;
    return __atomic_add_fetch(&g_live, 1, __ATOMIC_RELAXED);
}
static long long live_dec(void) {
    if (!g_live_track) return 0;
    return __atomic_sub_fetch(&g_live, 1, __ATOMIC_RELAXED);
}
static long long live_load(void) {
    if (!g_live_track) return 0;
    return __atomic_load_n(&g_live, __ATOMIC_RELAXED);
}
#else
static long long live_inc(void) { if (!g_live_track) return 0; return ++g_live; }
static long long live_dec(void) { if (!g_live_track) return 0; return --g_live; }
static long long live_load(void) { return g_live_track ? g_live : 0; }
#endif

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
#define POOL_ALIGN 8u
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
/* env config is read once: pool_take/obj_free/sweep are hot paths */
static long long g_pool_ttl_ms = POOL_DEFAULT_TTL_MS;
static size_t g_pool_max_bytes = (size_t)POOL_DEFAULT_MAX_MB * 1024u * 1024u;
static int g_pool_cfg_inited = 0;
static long long g_pool_last_sweep_ms = 0;

static void pool_cfg_init(void) {
    const char *e;
    if (g_pool_cfg_inited) return;
    e = getenv("WELL_POOL_TTL_MS");
    g_pool_ttl_ms = (e && e[0]) ? atoll(e) : POOL_DEFAULT_TTL_MS;
    e = getenv("WELL_POOL_MAX_MB");
    if (e && e[0]) {
        long long mb = atoll(e);
        g_pool_max_bytes = mb > 0 ? (size_t)mb * 1024u * 1024u : 0;
    }
    g_pool_cfg_inited = 1;
}

static size_t pool_class(size_t size) {
    return (size + POOL_ALIGN - 1u) / POOL_ALIGN;
}

/* ---------- per-thread pool cache ----------
 * The global pool lock was taken on every object/String allocation and release.
 * Each thread now keeps a small bounded free list per size class; it only
 * touches the global pool when the local list is empty/full, or when it exits.
 * The TLS key is created in well_init() (single-threaded) 鈥?creating it lazily
 * from two connection threads let them read each other's cache. */
#define POOL_TLS_PER_CLASS 32
#define POOL_TLS_MAX_BYTES (256u * 1024u)

typedef struct WellPoolTls {
    PoolNode *head[POOL_CLASSES];
    int count[POOL_CLASSES];
    size_t bytes;
} WellPoolTls;

static WellTlsKey g_pool_tls_key = WELL_TLS_KEY_INVALID;
static int g_pool_tls_enabled = 1;
static int g_pool_tls_cfg = 0;

static size_t pool_limit_bytes(void);
static void pool_tls_flush(WellPoolTls *t);

static void pool_tls_cfg_init(void) {
    const char *e;
    if (g_pool_tls_cfg) return;
    g_pool_tls_cfg = 1;
    e = getenv("WELL_POOL_TLS");
    if (e && e[0] && atoi(e) == 0) g_pool_tls_enabled = 0;
}

static void pool_tls_destructor(void *p) {
    if (!p) return;
    pool_tls_flush((WellPoolTls *)p);
    free(p);
}

static void pool_tls_ensure(void) {
    pool_tls_cfg_init();
    if (!g_pool_tls_enabled) return;
    if (g_pool_tls_key == WELL_TLS_KEY_INVALID)
        g_pool_tls_key = well_tls_key_create(pool_tls_destructor);
}

static WellPoolTls *pool_tls_get(int create) {
    WellPoolTls *t;
    if (!g_pool_tls_enabled || g_pool_tls_key == WELL_TLS_KEY_INVALID) return NULL;
    t = (WellPoolTls *)well_tls_get(g_pool_tls_key);
    if (t || !create) return t;
    t = (WellPoolTls *)calloc(1, sizeof(WellPoolTls));
    if (!t) return NULL;
    well_tls_set(g_pool_tls_key, t);
    return t;
}

/* Return cached blocks to the global pool; used at thread exit/shutdown. */
static void pool_tls_flush(WellPoolTls *t) {
    int c;
    size_t limit;
    if (!t) return;
    limit = pool_limit_bytes();
    well_lock_acquire(&g_pool_lock);
    for (c = 1; c < (int)POOL_CLASSES; c++) {
        PoolNode *n = t->head[c];
        while (n) {
            PoolNode *next = n->next;
            if (g_pool_bytes + n->size <= limit) {
                n->next = g_pool[c];
                g_pool[c] = n;
                g_pool_bytes += n->size;
            } else {
                free(n);
            }
            n = next;
        }
        t->head[c] = NULL;
        t->count[c] = 0;
    }
    t->bytes = 0;
    well_lock_release(&g_pool_lock);
}

/* Windows TLS slots have no destructor; the thread wrapper calls this. */
void well_pool_thread_cleanup(void) {
    WellPoolTls *t;
    if (g_pool_tls_key == WELL_TLS_KEY_INVALID) return;
    t = (WellPoolTls *)well_tls_get(g_pool_tls_key);
    if (!t) return;
    well_tls_set(g_pool_tls_key, NULL);
    pool_tls_flush(t);
    free(t);
}

static void *pool_take(size_t size) {
    size_t c = pool_class(size);
    PoolNode *n;
    WellPoolTls *t;
    pool_cfg_init();
    if (size > POOL_MAX_SIZE) return NULL;
    pool_tls_ensure();
    t = pool_tls_get(0);
    if (t) {
        n = t->head[c];
        if (n) {
            t->head[c] = n->next;
            t->count[c]--;
            t->bytes -= n->size;
            return n;
        }
    }
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
    WellPoolTls *t;
    if (!o) return;
    if (o->flags & WELL_FLAG_STACK) return; /* C-frame storage, not heap */
    size = o->alloc_size;
    if (size == 0 || size > POOL_MAX_SIZE) { free(o); return; }
    c = pool_class(size);
    n = (PoolNode *)o;
    n->size = c * POOL_ALIGN;
    pool_tls_ensure();
    t = pool_tls_get(1);
    if (t && t->count[c] < POOL_TLS_PER_CLASS &&
        t->bytes + n->size <= POOL_TLS_MAX_BYTES) {
        n->next = t->head[c];
        t->head[c] = n;
        t->count[c]++;
        t->bytes += n->size;
        return;
    }
    well_lock_acquire(&g_pool_lock);
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
    long long now;
    int i;
    pool_cfg_init();
    if (g_pool_ttl_ms <= 0) return;
    now = well_mono_ms();
    /* called from the scheduler loop: at most once per second */
    if (now - g_pool_last_sweep_ms < 1000) return;
    g_pool_last_sweep_ms = now;
    well_lock_acquire(&g_pool_lock);
    for (i = 1; i < (int)POOL_CLASSES; i++) {
        PoolNode **pp = &g_pool[i];
        while (*pp) {
            PoolNode *n = *pp;
            if (now - n->inserted_ms >= g_pool_ttl_ms) {
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
    pool_cfg_init();
    return g_pool_max_bytes;
}

static void well_pool_shutdown(void) {
    int i;
    well_pool_thread_cleanup();
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
/* Panic target for the current thread (setjmp/longjmp boundary per task). */
static WellTlsKey g_panic_key = WELL_TLS_KEY_INVALID;
static void panic_tls_ensure(void) {
    if (g_panic_key == WELL_TLS_KEY_INVALID) g_panic_key = well_tls_key_create(NULL);
}
static jmp_buf *panic_env_get(void) {
    return (jmp_buf *)well_tls_get(g_panic_key);
}
static void panic_env_set(jmp_buf *p) {
    panic_tls_ensure();
    well_tls_set(g_panic_key, p);
}
static WellMutex g_print_mutex;
static int g_print_inited = 0;

/* ---------- atomic RC ---------- */
/* 1 until the first thread is started (assembly + controller run). */
static volatile int g_rc_single = 1;
void well_rc_mark_multithreaded(void) { g_rc_single = 0; }

#ifdef _WIN32
static long well_atomic_load(WellAtomicInt *p) {
    return InterlockedCompareExchange((volatile LONG *)p, 0, 0);
}
static long well_atomic_inc(WellAtomicInt *p) {
#ifdef __TINYC__
    /* TinyCC's InterlockedIncrement returns the OLD value; normalize to the
     * new count so well_retain() can use one RMW. */
    return InterlockedIncrement((volatile LONG *)p) + 1;
#else
    return InterlockedIncrement((volatile LONG *)p);
#endif
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
long long well_int_div(long long a, long long b) {
    if (b == 0) well_panic("integer division by zero");
    if (a == (-9223372036854775807LL - 1) && b == -1) return a; /* avoid C UB */
    return a / b;
}

long long well_int_mod(long long a, long long b) {
    if (b == 0) well_panic("integer modulo by zero");
    if (a == (-9223372036854775807LL - 1) && b == -1) return 0; /* avoid C UB */
    return a % b;
}

/* Source position of the statement currently running on this thread.
 *
 * Generated code emits one well_source_set before every potentially panicking
 * statement, so this sits on the hot path.  The v2 scheme did two OS TLS
 * writes per call (~6.5 ns measured); one TLS read of a per-thread position
 * struct is ~1.5 ns and needs no compiler TLS support (TinyCC, MinGW __thread
 * emutls and __declspec(thread)-ignoring GCC builds all behave consistently).
 * Linux ELF targets use native compiler TLS, which is free. */
typedef struct {
    const char *file;
    int line;
} WellSrcPos;

#if !defined(_WIN32) && defined(__GNUC__) && !defined(__TINYC__)
#define WELL_NATIVE_SRC_TLS 1
static __thread WellSrcPos g_src_pos;
static void src_tls_ensure(void) {}
void well_source_set(const char *file, int line) {
    g_src_pos.file = file;
    g_src_pos.line = line;
}
static const char *src_file_get(void) { return g_src_pos.file; }
static int src_line_get(void) { return g_src_pos.line; }
#else
static WellTlsKey g_src_pos_key = WELL_TLS_KEY_INVALID;
static void src_tls_ensure(void) {
    if (g_src_pos_key == WELL_TLS_KEY_INVALID)
        g_src_pos_key = well_tls_key_create(free);
}
static WellSrcPos *src_pos(void) {
    WellSrcPos *p;
    if (g_src_pos_key == WELL_TLS_KEY_INVALID) src_tls_ensure();
    if (g_src_pos_key == WELL_TLS_KEY_INVALID) return NULL;
    p = (WellSrcPos *)well_tls_get(g_src_pos_key);
    if (!p) {
        p = (WellSrcPos *)calloc(1, sizeof(WellSrcPos));
        if (p) well_tls_set(g_src_pos_key, p);
    }
    return p;
}
void well_source_set(const char *file, int line) {
    WellSrcPos *p = src_pos();
    if (p) { p->file = file; p->line = line; }
}
static const char *src_file_get(void) {
    WellSrcPos *p = src_pos();
    return p ? p->file : NULL;
}
static int src_line_get(void) {
    WellSrcPos *p = src_pos();
    return p ? p->line : 0;
}
#endif

const char *well_source_file(void) { return src_file_get(); }
int well_source_line(void) { return src_line_get(); }

/* Per-thread IdUtil block cache (see well_id_util_get_id). */
static WellTlsKey g_id_cache_key = WELL_TLS_KEY_INVALID;
static void id_cache_key_ensure(void);

void well_panic(const char *msg) {
    jmp_buf *env = panic_env_get();
    const char *file = src_file_get();
    int line = src_line_get();
    if (file && line > 0)
        fprintf(stderr, "well panic: %s:%d: %s\n", file, line, msg);
    else
        fprintf(stderr, "well panic: %s\n", msg);
    fflush(stderr);
    if (env) longjmp(*env, 1);
    exit(WELL_PANIC_EXIT_CODE);
}

jmp_buf *well_panic_push(jmp_buf *env) {
    jmp_buf *old = panic_env_get();
    panic_env_set(env);
    return old;
}

void well_panic_pop(jmp_buf *old) {
    panic_env_set(old);
}

/* ---------- object graph ----------
 * The runtime does not maintain a reference graph: cycle reclamation is done
 * by oid-group TTL cleanup. These stubs keep the historical call sites cheap;
 * a future cycle collector can re-introduce the edge lists. */
void well_graph_add_edge(WellObj *from, WellObj *to) {
    (void)from;
    (void)to;
}

void well_graph_remove_edge(WellObj *from, WellObj *to) {
    (void)from;
    (void)to;
}

static void bytes_destroy(void *p);
static void temp_tls_ensure(void);

void well_init(void) {
    live_cfg_init();
    pool_cfg_init();
    /* Create the TLS key before any worker/connection thread exists. */
    pool_tls_ensure();
    memset(g_destroy, 0, sizeof(g_destroy));
    memset(g_type_desc, 0, sizeof(g_type_desc));
    g_inited = 1;
    /* Allocate per-thread keys once, single-threaded, before workers start. */
    panic_tls_ensure();
    src_tls_ensure();
    temp_tls_ensure();
    id_cache_key_ensure();
    well_mutex_init(&g_print_mutex);
    g_print_inited = 1;
    well_register_type(WELL_T_STRING, string_destroy);
    well_register_type(WELL_T_BYTES, bytes_destroy);
    well_containers_register();
    well_event_init();
    well_model_init();
}

/* ---------- object / rc ---------- */
static void *well_alloc_obj(size_t size, unsigned int type_id, long long oid, int zero) {
    size_t alloc_size = size;
    WellObj *o;
    /* Allocate whole pool classes: a class can host several object sizes, so a
     * smaller allocation must never be reused for a larger object. */
    if (size <= POOL_MAX_SIZE) alloc_size = pool_class(size) * POOL_ALIGN;
    o = (WellObj *)pool_take(size);
    if (o) {
        if (zero) memset(o, 0, size);
    } else {
        o = zero ? (WellObj *)calloc(1, alloc_size)
                 : (WellObj *)malloc(alloc_size);
    }
    if (!o) well_panic("out of memory (well_new)");
    o->rc = 1;
    o->type_id = type_id;
    o->oid = oid;
    o->flags = 0;
    o->exec_pins = NULL;
    o->reg_next = NULL;
    o->reg_prev = NULL;
    o->alloc_size = (alloc_size <= POOL_MAX_SIZE) ? (unsigned int)alloc_size : 0u;
    live_inc();
    return o;
}

void *well_new(size_t size, unsigned int type_id, long long oid) {
    WellObj *o = (WellObj *)well_alloc_obj(size, type_id, oid, 1);
    well_registry_add(o);
    return o;
}

void *well_new_uninit(size_t size, unsigned int type_id, long long oid) {
    WellObj *o = (WellObj *)well_alloc_obj(size, type_id, oid, 0);
    well_registry_add(o);
    return o;
}

void *well_new_uninit_unregistered(size_t size, unsigned int type_id, long long oid) {
    WellObj *o = (WellObj *)well_alloc_obj(size, type_id, oid, 0);
    o->flags |= WELL_FLAG_UNREGISTERED;
    return o;
}

/* Zeroed, unregistered object: used for inline HTTP route events that are
 * created and released on the same thread before anyone can look them up by
 * oid.  Model.set (well_register_obj) registers them if a handler stores one. */
void *well_new_zero_unregistered(size_t size, unsigned int type_id, long long oid) {
    WellObj *o = (WellObj *)well_alloc_obj(size, type_id, oid, 1);
    o->flags |= WELL_FLAG_UNREGISTERED;
    return o;
}

void well_register_obj(WellObj *o) {
    if (!o || !(o->flags & WELL_FLAG_UNREGISTERED)) return;
    o->flags &= ~WELL_FLAG_UNREGISTERED;
    well_registry_add(o);
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
    /* Single-threaded phase (assembly/controller run before any thread is
     * started): RC cannot be observed by another thread, so a plain
     * increment is equivalent and avoids the locked RMW.  The first
     * well_thread_start() turns this off for the rest of the process. */
    if (g_rc_single) {
        long r = o->rc;
        o->rc = r + 1;
        if (r == 0) well_panic("retain of dead object");
        return;
    }
    /* Single atomic RMW; a valid object had rc >= 1 before the increment. */
    if (well_atomic_inc(&o->rc) == 1) well_panic("retain of dead object");
}

void well_release(void *obj) {
    WellObj *o = (WellObj *)obj;
    long rc;
    if (!o) return;
    if (o->flags & WELL_FLAG_IMMORTAL) return;
    /* See well_retain(): plain count while the process is single-threaded. */
    if (g_rc_single) {
        rc = --o->rc;
    } else {
        /* One atomic RMW instead of load-then-decrement.  A double release still
         * trips the negative-count panic below. */
        rc = well_atomic_dec(&o->rc);
    }
    if (rc < 0) well_panic("release of dead object");
    if (rc == 0) {
        live_dec();
        /* Strings are never in the oid registry nor event payload pins (both
         * are oid-owned objects).  Skipping both saves a registry-shard
         * spinlock per String release. */
        if (o->type_id != WELL_T_STRING) {
            if (o->exec_pins) well_event_unpin_payload(o);
            if (!(o->flags & WELL_FLAG_UNREGISTERED)) well_registry_remove(o);
        }
        if (o->type_id < WELL_MAX_TYPES && g_destroy[o->type_id])
            g_destroy[o->type_id](o);
        else
            free(o);
    }
}

/* ---------- per-frame temporary ownership ----------
 * Function arguments built from owned expressions (e.g. f(s.trim())) are
 * evaluated into this per-frame stack; the callee's prologue records a mark
 * and releases everything above it on return. */

#define WELL_TEMP_MAX 1024
typedef struct {
    void *items[WELL_TEMP_MAX];
    int n;
} WellTempStack;

static WellTlsKey g_temps_key = WELL_TLS_KEY_INVALID;
static void temp_tls_ensure(void) {
    if (g_temps_key == WELL_TLS_KEY_INVALID) g_temps_key = well_tls_key_create(free);
}
static WellTempStack *temp_stack(void) {
    WellTempStack *t;
    temp_tls_ensure();
    if (g_temps_key == WELL_TLS_KEY_INVALID) return NULL;
    t = (WellTempStack *)well_tls_get(g_temps_key);
    if (!t) {
        t = (WellTempStack *)calloc(1, sizeof(WellTempStack));
        if (!t) return NULL;
        well_tls_set(g_temps_key, t);
    }
    return t;
}

/* Called when a thread ends (Windows TLS slots have no destructor). */
void well_temp_thread_cleanup(void) {
    WellTempStack *t;
    if (g_id_cache_key != WELL_TLS_KEY_INVALID) {
        void *idc = well_tls_get(g_id_cache_key);
        if (idc) {
            well_tls_set(g_id_cache_key, NULL);
            free(idc);
        }
    }
#ifndef WELL_NATIVE_SRC_TLS
    if (g_src_pos_key != WELL_TLS_KEY_INVALID) {
        void *pos = well_tls_get(g_src_pos_key);
        if (pos) {
            well_tls_set(g_src_pos_key, NULL);
            free(pos);
        }
    }
#endif
    if (g_temps_key == WELL_TLS_KEY_INVALID) return;
    t = (WellTempStack *)well_tls_get(g_temps_key);
    if (!t) return;
    well_tls_set(g_temps_key, NULL);
    free(t);
}

void *well_temp_hold(void *p) {
    WellTempStack *t = temp_stack();
    if (p && t && t->n < WELL_TEMP_MAX) t->items[t->n++] = p;
    return p;
}

int well_temp_mark(void) {
    WellTempStack *t = temp_stack();
    return t ? t->n : 0;
}

void well_temp_release_to(int mark) {
    WellTempStack *t = temp_stack();
    if (!t) return;
    if (mark < 0) mark = 0;
    if (mark > t->n) mark = t->n;
    while (t->n > mark) {
        void *p = t->items[--t->n];
        well_release(p);
    }
}

void well_temp_reset(void) { well_temp_release_to(0); }

/* ---------- frame journal ---------- */
void well_ctx_init(WellCtx *ctx, long long oid) {
    ctx->oid = oid;
    ctx->refs = NULL;
    ctx->n = 0;
    ctx->cap = 0;
    ctx->pins = NULL;
    ctx->pin_n = 0;
    ctx->pin_cap = 0;
    ctx->heap_refs = 0;
    ctx->heap_pins = 0;
}

/* Grow the frame journal, starting from the inline storage. */
static void ctx_refs_reserve(WellCtx *ctx, int need) {
    int ncap;
    void **nr;
    if (ctx->cap >= need) return;
    if (!ctx->refs) {
        ctx->refs = ctx->inline_refs;
        ctx->cap = WELL_CTX_INLINE_REFS;
        if (ctx->cap >= need) return;
    }
    ncap = ctx->cap * 2;
    while (ncap < need) ncap *= 2;
    nr = (void **)malloc((size_t)ncap * sizeof(void *));
    if (!nr) well_panic("out of memory (ctx refs)");
    memcpy(nr, ctx->refs, (size_t)ctx->n * sizeof(void *));
    if (ctx->heap_refs) free(ctx->refs);
    ctx->refs = nr;
    ctx->cap = ncap;
    ctx->heap_refs = 1;
}

static void ctx_push(WellCtx *ctx, void *obj) {
    ctx_refs_reserve(ctx, ctx->n + 1);
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
        if (!ctx->pins) {
            ctx->pins = ctx->inline_pins;
            ctx->pin_cap = WELL_CTX_INLINE_PINS;
        }
        if (ctx->pin_n == ctx->pin_cap) {
            int ncap = ctx->pin_cap * 2;
            WellOidPin *np = (WellOidPin *)malloc((size_t)ncap * sizeof(WellOidPin));
            if (!np) well_panic("out of memory (ctx pins)");
            memcpy(np, ctx->pins, (size_t)ctx->pin_n * sizeof(WellOidPin));
            if (ctx->heap_pins) free(ctx->pins);
            ctx->pins = np;
            ctx->pin_cap = ncap;
            ctx->heap_pins = 1;
        }
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

/* Bind a reference variable to a stable journal slot.
 *
 * The returned index is the binding's identity in the frame journal: every
 * later assignment to this variable updates exactly that entry, even when two
 * bindings currently hold the same object address.  That is what makes
 * ``a = b`` / parameter reassignment / null-then-assigned bindings safe; the
 * old pointer-scan implementation confused aliases.
 *
 * ``owned`` transfers the incoming reference-count edge to the frame (a fresh
 * object).  Otherwise the frame takes its own edge, so the binding stays valid
 * even if the source container/static is mutated during the frame.
 */
int well_ctx_bind(WellCtx *ctx, void **slot, int owned) {
    void *obj;
    int idx;
    if (!ctx || !slot) return -1;
    obj = *slot;
    if (obj && !owned) well_retain(obj);
    ctx_refs_reserve(ctx, ctx->n + 1);
    idx = ctx->n;
    ctx->refs[ctx->n++] = obj;
    return idx;
}

/* Replace a journalled binding by slot index (O(1), no pointer scan). */
void well_ctx_replace_at(WellCtx *ctx, int idx, void **slot, void *new_value, int owned) {
    void *old;
    if (!ctx || !slot) return;
    old = *slot;
    if (new_value && !owned) well_retain(new_value);
    if (idx >= 0 && idx < ctx->n) {
        ctx->refs[idx] = new_value;
    } else if (new_value) {
        /* Stale index (should not happen); keep ownership rather than leak. */
        well_track_owned(ctx, new_value);
    }
    if (!(old == new_value && owned)) well_release(old);
    *slot = new_value;
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
    if (ctx->heap_refs && ctx->refs) free(ctx->refs);
    ctx->refs = NULL;
    ctx->n = 0;
    ctx->cap = 0;
    ctx->heap_refs = 0;
    for (i = 0; i < ctx->pin_n; i++) {
        int k;
        for (k = 0; k < ctx->pins[i].count; k++) well_oid_unpin(ctx->pins[i].oid);
    }
    if (ctx->heap_pins && ctx->pins) free(ctx->pins);
    ctx->pins = NULL;
    ctx->pin_n = 0;
    ctx->pin_cap = 0;
    ctx->heap_pins = 0;
}

/* ---------- ownership ---------- */
#ifndef WELL_NO_OID_CHECK
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

/* Out-of-line violation path for the inlined well_check_oid_at() fast path.
 * Attaches the exact statement position before panicking. */
WELL_NORETURN void well_check_oid_fail(void *obj, WellCtx *ctx, const char *where,
                                       const char *file, int line) {
    WellObj *o = (WellObj *)obj;
    well_source_set(file, line);
    if (!o) well_panic("null object write");
    if (o->flags & WELL_FLAG_FROZEN) well_panic("write to frozen event");
    {
        char msg[256];
        snprintf(msg, sizeof(msg),
                 "well oid violation: %s (object oid=%lld, handler oid=%lld)",
                 where ? where : "write", o->oid, ctx ? ctx->oid : 0);
        well_panic(msg);
    }
}
#endif

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

/* Static fields are assembly-phase data: they belong to the startup ctx oid
 * (currently 0, but kept as a runtime value so it can change later), are
 * writable only before well_run_loop(), and read-only afterwards. ctX == NULL
 * is the trusted compiler-generated init path. */
static long long g_static_owner_oid = 0;
static int g_static_frozen = 0;

void well_static_owner_set(long long oid) { g_static_owner_oid = oid; }
long long well_static_owner_oid(void) { return g_static_owner_oid; }
void well_static_freeze(void) { g_static_frozen = 1; }

void well_static_check_write(WellCtx *ctx, const char *name) {
    if (!ctx) return;
    if (g_static_frozen || ctx->oid != g_static_owner_oid) {
        char msg[256];
        snprintf(msg, sizeof(msg),
                 "static field %s is read-only (assembly oid=%lld, ctx oid=%lld)",
                 name ? name : "?", g_static_owner_oid, ctx->oid);
        well_panic(msg);
    }
}

void well_static_set_ref(WellCtx *ctx, void **slot, void *val, int owned, const char *name) {
    void *old;
    if (!slot) return;
    well_static_check_write(ctx, name);
    if (val) {
        WellObj *vo = (WellObj *)val;
        if (vo->type_id != WELL_T_STRING && vo->oid != ctx->oid) {
            char msg[256];
            snprintf(msg, sizeof(msg),
                     "static field %s value oid %lld must equal assembly oid %lld",
                     name ? name : "?", vo->oid, ctx->oid);
            well_panic(msg);
        }
    }
    old = *slot;
    if (old == val) {
        if (owned && val) well_release(val);
        return;
    }
    *slot = val;
    if (val && !owned) well_retain(val);
    if (old) well_release(old);
}

#ifndef WELL_NO_OID_CHECK
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
#endif

/* ---------- Strings ---------- */
typedef struct {
    WellString **items;
    int n;
    int cap;
    /* Hash index over the interned literals.  Every string-literal expression
     * in generated C calls well_string_lit(), so the lookup is a hot path; the
     * original implementation scanned every literal with strcmp(), which made a
     * literal evaluation cost O(number of literals in the program).  `slots` is
     * an open-addressed table (power-of-two capacity, NULL = empty) published
     * together with items.  Old snapshots keep their own slot array, so readers
     * never observe a rehash. */
    WellString **slots;
    int slot_cap;
} WellLitTable;

/* The interned-literal table is read on every evaluation of a string literal
 * (codegen inlines well_string_lit() into worker/handler code) but appended to
 * only the first time a given literal is seen. Appending therefore uses a
 * copy-on-write snapshot: a published snapshot is immutable, so the lookup fast
 * path needs no lock; writers serialize on g_lits_lock and publish a new
 * snapshot descriptor. The items array is shared between snapshots while it has
 * spare capacity: an append writes slot `n`, which every older snapshot (with a
 * smaller `n`) never reads, so concurrent lock-free readers cannot observe a
 * half-written entry. Snapshots are process-lifetime and intentionally leaked
 * (same as the interned strings themselves). */
static WellLitTable *g_lits_pub = NULL;
static WellLock g_lits_lock = 0;

/* Acquire/release publication of the snapshot pointer. The fallback matches the
 * single-threaded fallback used by well_atomic_* for compilers without builtins
 * (e.g. TinyCC on POSIX). */
static WellLitTable *lits_pub_load(void) {
#ifdef _WIN32
    return (WellLitTable *)InterlockedCompareExchangePointer(
        (PVOID volatile *)&g_lits_pub, NULL, NULL);
#elif defined(__GNUC__) || defined(__clang__)
    return __atomic_load_n(&g_lits_pub, __ATOMIC_ACQUIRE);
#else
    return g_lits_pub;
#endif
}

static void lits_pub_store(WellLitTable *t) {
#ifdef _WIN32
    InterlockedExchangePointer((PVOID volatile *)&g_lits_pub, (PVOID)t);
#elif defined(__GNUC__) || defined(__clang__)
    __atomic_store_n(&g_lits_pub, t, __ATOMIC_RELEASE);
#else
    g_lits_pub = t;
#endif
}

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

/* Published literal-hash slots are read without the writer lock.  A slot write
 * is a single aligned pointer store, so a concurrent reader sees either NULL or
 * a fully initialized WellString (the string is filled in before publication). */
static WellString *lits_slot_load(WellString **slot) {
#ifdef _WIN32
    return (WellString *)InterlockedCompareExchangePointer(
        (PVOID volatile *)slot, NULL, NULL);
#elif defined(__GNUC__) || defined(__clang__)
    return __atomic_load_n(slot, __ATOMIC_ACQUIRE);
#else
    return *slot;
#endif
}

static void lits_slot_store(WellString **slot, WellString *s) {
#ifdef _WIN32
    InterlockedExchangePointer((PVOID volatile *)slot, (PVOID)s);
#elif defined(__GNUC__) || defined(__clang__)
    __atomic_store_n(slot, s, __ATOMIC_RELEASE);
#else
    *slot = s;
#endif
}

static void string_set_hash(WellString *s) {
    /* R-12: hash over the cached byte length, not strlen(): a String may hold
     * embedded NULs (Bytes.toString, substring, deserialize, JSON edge cases),
     * and the hash is used by Map key lookups together with well_string_eq(). */
    s->hash = hash_bytes(well_str_data(s), (size_t)s->len);
    s->hdr.flags |= WELL_FLAG_HASHED;
}

unsigned long long well_string_hash(WellString *s) {
    if (!s) return 0;
    if (s->hdr.flags & WELL_FLAG_HASHED) return s->hash;
    s->hash = hash_bytes(well_str_data(s), (size_t)s->len);
    s->hdr.flags |= WELL_FLAG_HASHED;
    return s->hash;
}

static void string_destroy(void *p) {
    WellString *s = (WellString *)p;
    /* data lives inline in the same allocation (see string_alloc_n); route the
     * block through the size-class pool so short-lived Strings do not hit
     * malloc/free on every request. */
    well_obj_free(s);
}

/* Allocate one block for a String header + inline bytes, using the size-class
 * pool when it fits (pool_take already handles the >4KB fallback as NULL). */
static WellString *string_block_alloc(size_t total) {
    WellString *s;
    static int pool_strings = -1;
    size_t block;
    if (pool_strings < 0) {
        const char *e = getenv("WELL_STRING_POOL");
        pool_strings = !(e && e[0] && atoi(e) == 0);
    }
    /* Round the allocation to the pool class even when the pool is empty and
     * this falls back to malloc(): well_obj_free() later classifies the block
     * from alloc_size, so a smaller-than-class malloc block would be reused for
     * a larger request and corrupt the heap.  (This exact mismatch caused
     * intermittent 0xC0000005 crashes under concurrent HTTP load.) */
    block = (total <= POOL_MAX_SIZE) ? pool_class(total) * POOL_ALIGN : total;
    s = (pool_strings && total <= POOL_MAX_SIZE) ? (WellString *)pool_take(total) : NULL;
    if (!s) {
        s = (WellString *)malloc(block);
        if (!s) well_panic("out of memory (String)");
        s->hdr.alloc_size = (total <= POOL_MAX_SIZE) ? block : 0;
    } else {
        s->hdr.alloc_size = block;
    }
    return s;
}

static WellString *string_alloc_n(const char *src, size_t n) {
    /* Header + bytes in one allocation: half the malloc/free calls for Strings. */
    WellString *s = string_block_alloc(sizeof(WellString) + n + 1);
    if (n && src) memcpy(well_str_data(s), src, n);
    well_str_data(s)[n] = 0;
    s->hdr.rc = 1;
    s->hdr.type_id = WELL_T_STRING;
    s->hdr.flags = 0;
    s->hdr.oid = 0;
    s->hdr.exec_pins = NULL;
    s->hdr.reg_next = NULL;
    s->hdr.reg_prev = NULL;
    s->hash = 0; /* computed lazily by well_string_hash() */
    s->len = (long long)n;
    live_inc();
    return s;
}

static WellString *string_alloc(const char *src) {
    return string_alloc_n(src, strlen(src ? src : ""));
}

/* Allocate an uninitialized byte buffer of length n (caller fills data and calls
 * string_set_hash).  Used by case conversion to avoid hashing twice. */
static WellString *string_alloc_raw(size_t n) {
    WellString *s = string_block_alloc(sizeof(WellString) + n + 1);
    well_str_data(s)[n] = 0;
    s->hdr.rc = 1;
    s->hdr.type_id = WELL_T_STRING;
    s->hdr.flags = 0;
    s->hdr.oid = 0;
    s->hdr.exec_pins = NULL;
    s->hdr.reg_next = NULL;
    s->hdr.reg_prev = NULL;
    s->hash = 0;
    s->len = (long long)n;
    live_inc();
    return s;
}

WellString *well_string_new(const char *src) {
    return string_alloc(src);
}

/* Internal (well_internal.h): build a String from an explicit byte count so
 * callers can preserve embedded NULs (R-12). */
WellString *well_string_new_n(const char *src, size_t n) {
    return string_alloc_n(src, n);
}

static WellString *lits_lookup_h(WellLitTable *t, const char *key,
                                 unsigned long long h) {
    int i, mask;
    if (!t) return NULL;
    if (t->slot_cap > 0 && t->slots) {
        mask = t->slot_cap - 1;
        i = (int)(h & (unsigned long long)mask);
        for (;;) {
            WellString *s = lits_slot_load(&t->slots[i]);
            if (!s) return NULL;
            if (s->hash == h && strcmp(well_str_data(s), key) == 0) return s;
            i = (i + 1) & mask;
        }
    }
    /* No index yet (only possible before the first insert): linear scan. */
    for (i = 0; i < t->n; i++) {
        if (t->items[i]->hash == h && strcmp(well_str_data(t->items[i]), key) == 0)
            return t->items[i];
    }
    return NULL;
}

/* Slow path: only reached the first time a literal is evaluated. Caller does
 * not hold g_lits_lock. The string is allocated before taking the lock so a
 * well_panic()/longjmp on OOM can never leave the spinlock held; re-checking
 * after the lock handles another worker interning the same literal meanwhile. */
static WellString *lits_insert(const char *key) {
    WellLitTable *t, *nt;
    WellString *s, *found;
    unsigned long long h;
    int n, cap, slot_cap;
    s = string_alloc(key);
    s->hdr.flags |= WELL_FLAG_IMMORTAL;
    live_dec(); /* literals are process-lifetime and not "live objects" */
    string_set_hash(s); /* literals are indexed by hash */
    h = s->hash;
    well_lock_acquire(&g_lits_lock);
    t = lits_pub_load();
    found = lits_lookup_h(t, key, h);
    if (found) {
        free(s); /* another worker interned it first */
        well_lock_release(&g_lits_lock);
        return found;
    }
    n = t ? t->n : 0;
    cap = t ? t->cap : 0;
    slot_cap = t ? t->slot_cap : 0;
    nt = (WellLitTable *)malloc(sizeof(WellLitTable));
    if (!nt) {
        well_lock_release(&g_lits_lock);
        well_panic("out of memory (literals)");
    }
    if (n == cap) {
        WellString **ni;
        int ncap = cap ? cap * 2 : 64;
        ni = (WellString **)malloc((size_t)ncap * sizeof(WellString *));
        if (!ni) {
            free(nt);
            well_lock_release(&g_lits_lock);
            well_panic("out of memory (literals)");
        }
        if (t && n > 0) memcpy(ni, t->items, (size_t)n * sizeof(WellString *));
        nt->items = ni;
        nt->cap = ncap;
    } else {
        /* Spare capacity: append into slot n of the shared array. Older
         * snapshots only scan indices < their own n, so slot n is invisible to
         * them until the new snapshot is published below. */
        nt->items = t->items;
        nt->cap = cap;
    }
    nt->items[n] = s;
    nt->n = n + 1;
    /* Grow/replace the index only when the load factor would exceed 0.6.
     * The new index includes the literal just appended. */
    if (slot_cap == 0 || (n + 1) * 10 >= slot_cap * 6) {
        int i;
        int ncap = 64;
        WellString **slots;
        while (ncap * 6 < (n + 1) * 10) ncap *= 2;
        slots = (WellString **)calloc((size_t)ncap, sizeof(WellString *));
        if (!slots) {
            free(nt);
            well_lock_release(&g_lits_lock);
            well_panic("out of memory (literals)");
        }
        for (i = 0; i <= n; i++) {
            unsigned long long sh = nt->items[i]->hash & (unsigned long long)(ncap - 1);
            while (lits_slot_load(&slots[sh])) sh = (sh + 1) & (unsigned long long)(ncap - 1);
            lits_slot_store(&slots[sh], nt->items[i]);
        }
        nt->slots = slots;
        nt->slot_cap = ncap;
    } else {
        int mask = slot_cap - 1;
        int i = (int)(h & (unsigned long long)mask);
        nt->slots = t->slots;
        nt->slot_cap = slot_cap;
        for (;;) {
            WellString *cur = lits_slot_load(&nt->slots[i]);
            if (!cur) {
                lits_slot_store(&nt->slots[i], s);
                break;
            }
            i = (i + 1) & mask;
        }
    }
    lits_pub_store(nt);
    well_lock_release(&g_lits_lock);
    return s;
}

WellString *well_string_lit(const char *src) {
    const char *key = src ? src : "";
    unsigned long long h;
    WellString *lit;
    if (!g_inited) well_init();
    h = hash_bytes(key, strlen(key));
    lit = lits_lookup_h(lits_pub_load(), key, h);
    return lit ? lit : lits_insert(key);
}

WellString *well_string_lit_slot(WellString **slot, const char *src) {
    WellString *v;
    if (!slot) return well_string_lit(src);
    v = *slot;
    if (v) return v;
    v = well_string_lit(src);
    *slot = v; /* benign race: all writers store the same interned pointer */
    return v;
}

WellString *well_string_concat(WellString *a, WellString *b) {
    const char *sa;
    const char *sb;
    size_t la, lb;
    WellString *s;
    /* Empty operands are common (s = "" + x); reuse the non-empty side. */
    if (!a || a->len == 0) {
        if (!b) return well_string_lit("");
        well_retain(b);
        return b;
    }
    if (!b || b->len == 0) {
        well_retain(a);
        return a;
    }
    sa = well_str_data(a);
    sb = well_str_data(b);
    la = (size_t)a->len;
    lb = (size_t)b->len;
    s = string_block_alloc(sizeof(WellString) + la + lb + 1);
    memcpy(well_str_data(s), sa, la);
    memcpy(well_str_data(s) + la, sb, lb);
    well_str_data(s)[la + lb] = 0;
    s->hdr.rc = 1;
    s->hdr.type_id = WELL_T_STRING;
    s->hdr.flags = 0;
    s->hdr.oid = 0;
    s->hdr.exec_pins = NULL;
    s->hdr.reg_next = NULL;
    s->hdr.reg_prev = NULL;
    s->len = (long long)(la + lb);
    s->hash = 0;
    live_inc();
    return s;
}

const char *well_string_cstr(WellString *s) {
    return s ? well_str_data(s) : "";
}

int well_string_eq(WellString *a, WellString *b) {
    if (a == b) return 1;
    if (!a || !b) return 0;
    /* R-12: compare the cached lengths and bytes, not strcmp(): strcmp stops at
     * an embedded NUL and would report "a\0b" == "a\0c". The cached hash is over
     * the full length, so a hash mismatch already proves inequality. */
    if (a->len != b->len) return 0;
    if (well_string_hash(a) != well_string_hash(b)) return 0;
    return a->len == 0 || memcmp(well_str_data(a), well_str_data(b), (size_t)a->len) == 0;
}

long long well_string_len(WellString *s) {
    return s ? s->len : 0;
}

/* ---------- Bytes: mutable byte buffer ---------- */

static void bytes_destroy(void *p) {
    WellBytes *b = (WellBytes *)p;
    if (!b) return;
    free(b->data);
    free(b);
}

WellBytes *well_bytes_new(WellCtx *ctx) {
    WellBytes *b = (WellBytes *)well_new(sizeof(WellBytes), WELL_T_BYTES, ctx ? ctx->oid : 0);
    b->data = NULL;
    b->len = 0;
    b->cap = 0;
    return b;
}

long long well_bytes_len(WellBytes *b) { return b ? b->len : 0; }

static void bytes_reserve(WellBytes *b, long long need) {
    long long cap;
    unsigned char *n;
    if (!b) return;
    if (need <= b->cap) return;
    cap = b->cap > 0 ? b->cap : 32;
    while (cap < need) cap *= 2;
    n = (unsigned char *)realloc(b->data, (size_t)cap);
    if (!n) well_panic("out of memory (Bytes)");
    b->data = n;
    b->cap = cap;
}

void well_bytes_push(WellBytes *b, int byte, WellCtx *ctx) {
    if (!b) well_panic("push on null Bytes");
    well_check_oid((void *)b, ctx, "Bytes.push");
    bytes_reserve(b, b->len + 1);
    b->data[b->len++] = (unsigned char)(byte & 0xFF);
}

void well_bytes_append(WellBytes *b, WellBytes *src, WellCtx *ctx) {
    if (!b) well_panic("append on null Bytes");
    well_check_oid((void *)b, ctx, "Bytes.append");
    if (!src || src->len == 0) return;
    bytes_reserve(b, b->len + src->len);
    memcpy(b->data + b->len, src->data, (size_t)src->len);
    b->len += src->len;
}

void well_bytes_append_str(WellBytes *b, WellString *s, WellCtx *ctx) {
    size_t n;
    if (!b) well_panic("appendStr on null Bytes");
    well_check_oid((void *)b, ctx, "Bytes.appendStr");
    if (!s) return;
    /* R-12: use the cached length; strlen() would cut the string at a NUL. */
    n = (size_t)s->len;
    if (n == 0) return;
    bytes_reserve(b, b->len + (long long)n);
    memcpy(b->data + b->len, well_str_data(s), n);
    b->len += (long long)n;
}

long long well_bytes_get(WellBytes *b, long long i) {
    if (!b || i < 0 || i >= b->len) return -1;
    return (long long)b->data[i];
}

void well_bytes_set(WellBytes *b, long long i, int byte, WellCtx *ctx) {
    if (!b) well_panic("set on null Bytes");
    well_check_oid((void *)b, ctx, "Bytes.set");
    if (i < 0 || i >= b->len) well_panic("Bytes.set: index out of range");
    b->data[i] = (unsigned char)(byte & 0xFF);
}

void well_bytes_clear(WellBytes *b, WellCtx *ctx) {
    if (!b) return;
    well_check_oid((void *)b, ctx, "Bytes.clear");
    b->len = 0;
}

/* Treats the buffer as text (NUL terminated); binary payloads should stay Bytes. */
WellString *well_bytes_to_string(WellBytes *b) {
    if (!b) return well_string_lit("");
    return string_alloc_n((const char *)b->data, (size_t)b->len);
}

/* ---------- take helpers: compute from an owned temporary, then release it ---------- */

long long well_str_len_take(WellString *s) {
    long long n = well_string_len(s);
    well_release(s);
    return n;
}

long long well_str_byte_at_take(WellString *s, long long i) {
    long long v = well_str_byte_at(s, i);
    well_release(s);
    return v;
}

long long well_bytes_len_take(WellBytes *b) {
    long long n = well_bytes_len(b);
    well_release(b);
    return n;
}

long long well_bytes_get_take(WellBytes *b, long long i) {
    long long v = well_bytes_get(b, i);
    well_release(b);
    return v;
}

long long well_array_len_take(WellArray *a) {
    long long n = well_array_len(a);
    well_release(a);
    return n;
}

int well_str_eq_take(WellString *a, int a_owned, WellString *b, int b_owned) {
    int eq = well_string_eq(a, b);
    if (a_owned) well_release(a);
    if (b_owned) well_release(b);
    return eq;
}

WellString *well_string_concat_take(WellString *a, int a_owned, WellString *b, int b_owned) {
    WellString *s = well_string_concat(a, b);
    if (a_owned) well_release(a);
    if (b_owned) well_release(b);
    return s;
}

WellString *well_str_substring_take(WellString *s, long long start, long long end) {
    WellString *r = well_str_substring(s, start, end);
    well_release(s);
    return r;
}

WellString *well_str_replace(WellString *s, WellString *old_s, WellString *new_s) {
    const char *d;
    size_t n, on, nn, count = 0, i, k = 0, outlen;
    char *buf;
    if (!s) return well_string_lit("");
    if (!old_s || !new_s) well_panic("String.replace: arguments must be String");
    if (old_s->len <= 0) well_panic("String.replace: old must not be empty");
    d = well_str_data(s);
    n = (size_t)s->len;
    on = (size_t)old_s->len;
    nn = (size_t)new_s->len;
    for (i = 0; i + on <= n;) {
        if (memcmp(d + i, well_str_data(old_s), on) == 0) { count++; i += on; }
        else i++;
    }
    if (count == 0) { well_retain(s); return s; }
    outlen = n + count * nn - count * on;
    buf = (char *)malloc(outlen + 1);
    if (!buf) well_panic("out of memory (String.replace)");
    for (i = 0; i + on <= n;) {
        if (memcmp(d + i, well_str_data(old_s), on) == 0) {
            memcpy(buf + k, well_str_data(new_s), nn);
            k += nn;
            i += on;
        } else {
            buf[k++] = d[i++];
        }
    }
    if (i < n) { memcpy(buf + k, d + i, n - i); k += n - i; }
    buf[k] = '\0';
    {
        WellString *r = string_alloc_n(buf, (long long)k);
        free(buf);
        return r;
    }
}

WellString *well_str_replace_take(WellString *s, WellString *old_s, WellString *new_s) {
    WellString *r = well_str_replace(s, old_s, new_s);
    well_release(s);
    return r;
}

WellString *well_str_trim_take(WellString *s) {
    WellString *r = well_str_trim(s);
    well_release(s);
    return r;
}

WellArray *well_str_split_take(WellString *s, WellString *sep, WellCtx *ctx) {
    WellArray *r = well_str_split(s, sep, ctx);
    well_release(s);
    return r;
}

long long well_str_index_of_take(WellString *s, WellString *needle) {
    long long i = well_str_index_of(s, needle);
    well_release(s);
    return i;
}

long long well_str_index_of_from_take(WellString *s, WellString *needle, long long from) {
    long long i = well_str_index_of_from(s, needle, from);
    well_release(s);
    return i;
}

int well_str_contains_take(WellString *s, WellString *needle) {
    int v = well_str_contains(s, needle);
    well_release(s);
    return v;
}

int well_str_starts_with_take(WellString *s, WellString *prefix) {
    int v = well_str_starts_with(s, prefix);
    well_release(s);
    return v;
}

WellString *well_str_to_lower_take(WellString *s) {
    WellString *r = well_str_to_lower(s);
    well_release(s);
    return r;
}

WellString *well_str_to_upper_take(WellString *s) {
    WellString *r = well_str_to_upper(s);
    well_release(s);
    return r;
}

int well_str_ends_with_take(WellString *s, WellString *suffix) {
    int v = well_str_ends_with(s, suffix);
    well_release(s);
    return v;
}



long long well_str_byte_at(WellString *s, long long i) {
    if (!s || i < 0 || i >= s->len) return -1;
    return (long long)(unsigned char)well_str_data(s)[i];
}

long long well_str_code_point_at(WellString *s, long long i) {
    const unsigned char *p;
    unsigned char b0;
    unsigned cp;
    int len, k;
    if (!s || i < 0 || i >= s->len) return -1;
    p = (const unsigned char *)well_str_data(s) + i;
    b0 = p[0];
    if (b0 < 0x80) return (long long)b0;
    if (b0 >= 0xC2 && b0 <= 0xDF) { len = 2; cp = b0 & 0x1F; }
    else if (b0 >= 0xE0 && b0 <= 0xEF) { len = 3; cp = b0 & 0x0F; }
    else if (b0 >= 0xF0 && b0 <= 0xF4) { len = 4; cp = b0 & 0x07; }
    else return -1;
    if (i + len > s->len) return -1;
    for (k = 1; k < len; k++) {
        unsigned char c = p[k];
        if ((c & 0xC0) != 0x80) return -1;
        cp = (cp << 6) | (unsigned)(c & 0x3F);
    }
    if (len == 3 && cp < 0x800) return -1;
    if (len == 4 && cp < 0x10000) return -1;
    if (cp >= 0xD800 && cp <= 0xDFFF) return -1;
    if (cp > 0x10FFFF) return -1;
    return (long long)cp;
}

long long well_str_code_point_at_take(WellString *s, long long i) {
    long long v = well_str_code_point_at(s, i);
    well_release(s);
    return v;
}

WellString *well_string_from_code_point(long long cp) {
    char buf[5];
    int n;
    if (cp < 0 || cp > 0x10FFFF || (cp >= 0xD800 && cp <= 0xDFFF))
        cp = 0xFFFD;
    if (cp < 0x80) {
        buf[0] = (char)cp;
        n = 1;
    } else if (cp < 0x800) {
        buf[0] = (char)(0xC0 | (cp >> 6));
        buf[1] = (char)(0x80 | (cp & 0x3F));
        n = 2;
    } else if (cp < 0x10000) {
        buf[0] = (char)(0xE0 | (cp >> 12));
        buf[1] = (char)(0x80 | ((cp >> 6) & 0x3F));
        buf[2] = (char)(0x80 | (cp & 0x3F));
        n = 3;
    } else {
        buf[0] = (char)(0xF0 | (cp >> 18));
        buf[1] = (char)(0x80 | ((cp >> 12) & 0x3F));
        buf[2] = (char)(0x80 | ((cp >> 6) & 0x3F));
        buf[3] = (char)(0x80 | (cp & 0x3F));
        n = 4;
    }
    buf[n] = 0;
    return well_string_new(buf);
}

WellString *well_str_from_byte(int code) {
    char c = (char)(unsigned char)(code & 0xFF);
    return string_alloc_n(&c, 1);
}

WellString *well_str_substring(WellString *s, long long start, long long end) {
    const char *d = s ? well_str_data(s) : "";
    long long n = well_string_len(s);
    if (start < 0) start = 0;
    if (end > n) end = n;
    if (start > end) start = end;
    return string_alloc_n(d + start, (size_t)(end - start));
}

/* Length-aware substring search (a portable memmem for the String helpers).
 * R-12: strstr()/strlen() stop at an embedded NUL, so all matching here works
 * on the cached lengths instead. Returns the byte offset or -1. */
static long long mem_find(const char *hay, size_t hay_len,
                          const char *needle, size_t needle_len) {
    size_t i;
    if (needle_len == 0) return 0;
    if (needle_len > hay_len) return -1;
    for (i = 0; i + needle_len <= hay_len; i++) {
        if (hay[i] == needle[0] && memcmp(hay + i, needle, needle_len) == 0)
            return (long long)i;
    }
    return -1;
}

long long well_str_index_of_from(WellString *s, WellString *needle, long long from) {
    const char *d = s ? well_str_data(s) : "";
    long long n = well_string_len(s);
    long long nn = needle ? needle->len : 0;
    long long hit;
    if (from < 0) from = 0;
    if (from > n) return -1;
    if (nn == 0) return from;
    if (n - from < nn) return -1;
    hit = mem_find(d + from, (size_t)(n - from), well_str_data(needle), (size_t)nn);
    return hit < 0 ? -1 : from + hit;
}

long long well_str_index_of(WellString *s, WellString *needle) {
    return well_str_index_of_from(s, needle, 0);
}

int well_str_contains(WellString *s, WellString *needle) {
    return well_str_index_of(s, needle) >= 0;
}

int well_str_starts_with(WellString *s, WellString *prefix) {
    const char *d = s ? well_str_data(s) : "";
    const char *p = prefix ? well_str_data(prefix) : "";
    long long pn = prefix ? prefix->len : 0;
    if (well_string_len(s) < pn) return 0;
    return memcmp(d, p, (size_t)pn) == 0;
}

int well_str_ends_with(WellString *s, WellString *suffix) {
    const char *d = s ? well_str_data(s) : "";
    const char *p = suffix ? well_str_data(suffix) : "";
    long long dn = well_string_len(s), pn = suffix ? suffix->len : 0;
    if (dn < pn) return 0;
    return memcmp(d + dn - pn, p, (size_t)pn) == 0;
}

static int str_is_space(char c) {
    return c == ' ' || c == '\t' || c == '\r' || c == '\n';
}

WellString *well_str_trim(WellString *s) {
    const char *d;
    size_t a = 0, b, n;
    if (!s) return well_string_lit("");
    d = well_str_data(s);
    n = (size_t)s->len;
    b = n;
    while (a < b && str_is_space(d[a])) a++;
    while (b > a && str_is_space(d[b - 1])) b--;
    if (a == 0 && b == n) {
        /* nothing to trim: share the original instead of copying */
        well_retain(s);
        return s;
    }
    return string_alloc_n(d + a, b - a);
}

/* Two digits per division; String.fromInt is on JSON/logging hot paths. */
static const char digits2[201] = "00010203040506070809101112131415161718192021222324252627282930313233343536373839404142434445464748495051525354555657585960616263646566676869707172737475767778798081828384858687888990919293949596979899";
WellString *well_str_from_int(long long v) {
    /* Hand-rolled conversion: the C runtime's snprintf("%lld") costs ~80ns on
     * Windows and String.fromInt/JSON/logging call this on every number. */
    char buf[24];
    char *p = buf + sizeof(buf);
    unsigned long long u;
    int neg = v < 0;
    if (neg) u = (unsigned long long)(-(v + 1)) + 1ULL;
    else u = (unsigned long long)v;
    do {
        unsigned rem = (unsigned)(u % 100ULL);
        u /= 100ULL;
        *--p = digits2[rem * 2 + 1];
        if (rem >= 10 || u) *--p = digits2[rem * 2];
    } while (u);
    if (neg) *--p = '-';
    return string_alloc_n(p, (size_t)(buf + sizeof(buf) - p));
}

WellString *well_str_to_lower(WellString *s) {
    size_t i, n;
    WellString *r;
    if (!s) return well_string_lit("");
    n = (size_t)s->len;
    r = string_alloc_raw(n);
    for (i = 0; i < n; i++) {
        char c = well_str_data(s)[i];
        well_str_data(r)[i] = (c >= 'A' && c <= 'Z') ? (char)(c - 'A' + 'a') : c;
    }
    string_set_hash(r); /* content changed: refresh the cached hash */
    return r;
}

WellString *well_str_to_upper(WellString *s) {
    size_t i, n;
    WellString *r;
    if (!s) return well_string_lit("");
    n = (size_t)s->len;
    r = string_alloc_raw(n);
    for (i = 0; i < n; i++) {
        char c = well_str_data(s)[i];
        well_str_data(r)[i] = (c >= 'a' && c <= 'z') ? (char)(c - 'a' + 'A') : c;
    }
    string_set_hash(r); /* content changed: refresh the cached hash */
    return r;
}

WellArray *well_str_split(WellString *s, WellString *sep, WellCtx *ctx) {
    WellArray *a = well_array_new(ctx, 1);
    const char *d = s ? well_str_data(s) : "";
    const char *sp = sep ? well_str_data(sep) : "";
    size_t spl = sep ? (size_t)sep->len : 0;
    size_t total = (size_t)well_string_len(s);
    size_t start = 0;
    WellValue v;
    /* R-12: split on the cached lengths; strstr() would stop at an embedded
     * NUL in either operand (and a NUL separator is a valid byte). */
    if (spl == 0) {
        v.p = string_alloc_n(d, total);
        well_array_push(a, v, ctx);
        well_release(v.p);
        return a;
    }
    for (;;) {
        long long hit = mem_find(d + start, total - start, sp, spl);
        size_t end = hit < 0 ? total : start + (size_t)hit;
        WellString *part = string_alloc_n(d + start, end - start);
        v.p = part;
        well_array_push(a, v, ctx);
        well_release(part);
        if (hit < 0) break;
        start = end + spl;
        if (start > total) break;
    }
    return a;
}



void well_shutdown(void) {
    if (getenv("WELL_TYPE_DEBUG")) well_type_debug_dump();
    well_timer_shutdown();
    well_module_shutdown_all();
    well_model_shutdown();
    well_event_shutdown();
    well_pool_shutdown();
    if (g_print_inited) {
        well_mutex_destroy(&g_print_mutex);
        g_print_inited = 0;
    }
    {
        const char *dbg = getenv("WELL_RC_DEBUG");
        if (dbg && dbg[0] && dbg[0] != '0') {
            fprintf(stderr, "well: live objects at shutdown = %lld\n", live_load());
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

/* Monotonic microsecond clock for benchmarks and fine-grained timing.
 *
 * Host.nowMs() is wall-clock (epoch) and, on Windows, GetSystemTimeAsFileTime
 * has a 10-15 ms tick: a 20 ms loop measurement is mostly quantisation noise.
 * QueryPerformanceCounter / CLOCK_MONOTONIC give sub-microsecond resolution. */
long long well_mono_us(void) {
#ifdef _WIN32
    static LARGE_INTEGER freq;
    static volatile int ready = 0;
    LARGE_INTEGER c;
    if (!ready) {
        QueryPerformanceFrequency(&freq);
        ready = 1;
    }
    QueryPerformanceCounter(&c);
    return (long long)((double)c.QuadPart * 1000000.0 / (double)freq.QuadPart);
#else
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (long long)ts.tv_sec * 1000000LL + (long long)ts.tv_nsec / 1000LL;
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

/* Id generator: the global lock used to be taken once per id (one per HTTP
 * request).  A thread now reserves a small block of sequence numbers for the
 * current millisecond under the lock and hands out the rest from thread-local
 * storage, so the lock is touched once per block instead of once per id.
 * Uniqueness is unchanged: every (timestamp, sequence) pair is handed out by
 * the shared allocator exactly once. */
#define WELL_ID_BLOCK 64
typedef struct {
    long long ts;
    int seq;
    int left;
} WellIdCache;

static void id_cache_key_ensure(void) {
    if (g_id_cache_key == WELL_TLS_KEY_INVALID)
        g_id_cache_key = well_tls_key_create(free);
}

static WellIdCache *id_cache(void) {
    WellIdCache *c;
    if (g_id_cache_key == WELL_TLS_KEY_INVALID) id_cache_key_ensure();
    if (g_id_cache_key == WELL_TLS_KEY_INVALID) return NULL;
    c = (WellIdCache *)well_tls_get(g_id_cache_key);
    if (!c) {
        c = (WellIdCache *)calloc(1, sizeof(WellIdCache));
        if (c) well_tls_set(g_id_cache_key, c);
    }
    return c;
}

static long long id_make(long long ts, int seq) {
    return ((ts - WELL_ID_EPOCH_MS) << WELL_ID_TIME_SHIFT) |
           ((long long)g_id_server << WELL_ID_SERVER_SHIFT) |
           (long long)seq;
}

static long long id_refill(long long ts) {
    WellIdCache *c = id_cache();
    long long id;
    int start, block;
    well_lock_acquire(&g_id_lock);
    if (g_id_server < 0) {
        const char *env = getenv("WELL_SERVER_ID");
        g_id_server = env && env[0] ? atoi(env) : 0;
        if (g_id_server < 0 || g_id_server > WELL_ID_SERVER_MAX) {
            well_lock_release(&g_id_lock);
            well_panic("WELL_SERVER_ID out of range (0..1023)");
        }
    }
    if (ts > g_id_last_ts) {
        g_id_last_ts = ts;
        g_id_seq = 0;
    } else {
        ts = g_id_last_ts;
    }
    if (g_id_seq >= WELL_ID_SEQ_MASK) {
        g_id_last_ts++;
        g_id_seq = 0;
        ts = g_id_last_ts;
    }
    start = g_id_seq + 1;
    block = WELL_ID_SEQ_MASK - g_id_seq; /* values still available in this ms */
    if (block > WELL_ID_BLOCK) block = WELL_ID_BLOCK;
    g_id_seq += block;
    id = id_make(ts, start);
    well_lock_release(&g_id_lock);
    if (c) {
        c->ts = ts;
        c->seq = start + 1;
        c->left = block - 1;
    }
    return id;
}

long long well_id_util_get_id(void) {
    WellIdCache *c = id_cache();
    long long ts = well_now_ms();
    if (ts < WELL_ID_EPOCH_MS) ts = WELL_ID_EPOCH_MS;
    /* A cached block belongs to one millisecond; a clock that jumped back is
     * clamped to the block's timestamp so ids never go backwards. */
    if (c && c->left > 0 && ts <= c->ts) {
        c->left--;
        return id_make(c->ts, c->seq++);
    }
    return id_refill(ts);
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
    return live_load();
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
    live_dec();
    well_obj_free(o);
}

/* Detach all objects of an oid from the registry (assembly oid is exempt).
 * The returned list must be passed to well_force_free_objects() *outside* any
 * global scheduler/activity locks. */
WellObj *well_oid_take_objects(long long oid) {
    if (oid == well_static_owner_oid()) return NULL;
    return well_registry_take(oid);
}

void well_force_free_objects(WellObj *p) {
    while (p) {
        WellObj *next = p->reg_next;
        force_free_obj(p);
        p = next;
    }
}

