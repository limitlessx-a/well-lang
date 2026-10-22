#ifndef WELLRT_H
#define WELLRT_H

#include <stddef.h>
#include <stdint.h>

#define WELL_T_STRING 1u
#define WELL_T_ARRAY 2u
#define WELL_T_MAP 3u
#define WELL_T_LINKED 4u
#define WELL_T_BYTES 5u
#define WELL_FLAG_IMMORTAL 1u
#define WELL_FLAG_IN_MODEL 2u
#define WELL_FLAG_FROZEN 4u

#if defined(_WIN32)
typedef volatile long WellAtomicInt;
#else
typedef volatile int WellAtomicInt;
#endif

typedef struct WellObj {
    WellAtomicInt rc;
    unsigned int type_id;
    unsigned int flags;
    long long oid;
    void *graph_edges;   /* internal: outgoing reference edges for DAG checks */
    void *exec_pins;     /* internal: event execution pins */
    struct WellObj *reg_next; /* internal: oid registry */
    struct WellObj *reg_prev;
    size_t alloc_size;
} WellObj;

typedef union WellValue {
    long long i;
    double d;
    void *p;
} WellValue;

typedef struct WellArray WellArray;
typedef struct WellMap WellMap;
typedef struct WellLinked WellLinked;
typedef struct WellLinkedNode WellLinkedNode;

typedef struct WellString {
    WellObj hdr;
    char *data;
    unsigned long long hash;
    long long len; /* cached byte length: len(s) is used in parsing loops */
} WellString;

/* Mutable byte buffer: build binary/text payloads without O(n^2) concatenation. */
typedef struct WellBytes {
    WellObj hdr;
    unsigned char *data;
    long long len;
    long long cap;
} WellBytes;

typedef struct WellOidPin {
    long long oid;
    int count;
} WellOidPin;

#define WELL_CTX_INLINE_REFS 16
#define WELL_CTX_INLINE_PINS 4
typedef struct WellCtx {
    long long oid;
    void **refs;
    int n;
    int cap;
    WellOidPin *pins;
    int pin_n;
    int pin_cap;
    /* inline storage avoids one malloc/free per handler for the common case */
    void *inline_refs[WELL_CTX_INLINE_REFS];
    int heap_refs;
    WellOidPin inline_pins[WELL_CTX_INLINE_PINS];
    int heap_pins;
} WellCtx;

/* runtime */
void well_init(void);
void well_shutdown(void);
void well_panic(const char *msg);

/* module registry: core does not include module headers */
typedef struct WellModule {
    const char *name;
    int  (*init)(void);
    int  (*start)(void);
    void (*stop)(void);
    void (*shutdown)(void);
} WellModule;

void well_module_register(const WellModule *module);
void well_module_init_all(void);
void well_module_shutdown_all(void);


/* builtin event factory registry (HTTP/WS/MySQL register their own events) */
typedef int WellBuiltinEventKind;
void well_builtin_event_register(WellBuiltinEventKind kind,
                                 unsigned int type_id,
                                 void *(*factory)(long long oid, void *payload));
void *well_builtin_event_new(WellBuiltinEventKind kind, long long oid, void *payload);

void *well_new(size_t size, unsigned int type_id, long long oid);
void well_register_type(unsigned int id, void (*destroy)(void *));
void well_retain(void *obj);
void well_release(void *obj);
/* per-frame ownership of temporaries used as call arguments */
void *well_temp_hold(void *p);
int well_temp_mark(void);
void well_temp_release_to(int mark);
void well_temp_reset(void);
void well_obj_free(void *obj);
long long well_live_count(void);

/* type metadata (generated for each struct/event) */
typedef enum {
    WELL_FIELD_INT = 1,
    WELL_FIELD_LONG,
    WELL_FIELD_DOUBLE,
    WELL_FIELD_BOOL,
    WELL_FIELD_STRING,
    WELL_FIELD_REF,
    WELL_FIELD_ARRAY,
    WELL_FIELD_MAP,
    WELL_FIELD_LINKED
} WellFieldKind;

typedef struct WellFieldDesc {
    const char *name;
    size_t offset;
    int kind;
    unsigned int type_id;      /* ref type id, or container type id */
    int elem_kind;             /* container element kind */
    unsigned int elem_type_id; /* container element ref type id */
} WellFieldDesc;

typedef struct WellTypeDesc {
    const char *name;
    unsigned int type_id;
    size_t size;
    int is_event;
    int field_count;
    const WellFieldDesc *fields;
} WellTypeDesc;

void well_register_type_desc(const WellTypeDesc *desc);
const WellTypeDesc *well_type_desc(unsigned int type_id);
const WellFieldDesc *well_type_field(const WellTypeDesc *desc, const char *name);
typedef void (*WellFieldVisitor)(void *field_ptr, const WellFieldDesc *field, void *user);
void well_visit_fields(void *obj, WellFieldVisitor fn, void *user);

/* row-level binary serialization (metadata-driven) */
typedef void *(*WellRefResolver)(unsigned int type_id, long long oid, void *user);
unsigned char *well_encode(void *obj, size_t *out_len);
void *well_decode(unsigned int type_id, long long oid,
                  const unsigned char *buf, size_t len,
                  WellRefResolver resolve, void *user);

/* JSON parsing (metadata-driven into a struct/event) */
void *well_json_parse(unsigned int type_id, const char *text, long long oid);

/* frame journal */
void well_ctx_init(WellCtx *ctx, long long oid);
void well_ctx_clear(WellCtx *ctx);
void well_ctx_pin(WellCtx *ctx, long long oid);
void well_ctx_unpin(WellCtx *ctx, long long oid);
void well_ctx_replace(WellCtx *ctx, void **slot, void *new_value, int owned);
int well_ctx_mark(WellCtx *ctx);
void well_ctx_release_from(WellCtx *ctx, int mark);
void well_track(WellCtx *ctx, void *obj);        /* borrow -> retain + track */
void well_track_owned(WellCtx *ctx, void *obj);  /* transfer ownership to frame */

/* ownership checks */
void well_check_oid(void *obj, WellCtx *ctx, const char *where);
void well_check_value_oid(void *target, void *value, WellCtx *ctx);
void well_set_ref(void *owner, void **slot, void *val);
/* static-class field write (no owner object / oid checks) */
void well_static_set_ref(void **slot, void *val, int owned);
void well_freeze(void *obj);

/* Model */
typedef struct WellStore WellStore;
struct WellStore {
    void *self;
    void *(*select)(void *self, unsigned int type_id, long long oid);
    int (*upsert)(void *self, void *obj);
    int (*delete_)(void *self, unsigned int type_id, long long oid);
    void (*shutdown)(void *self);
};

void well_model_init(void);
void well_model_use_store(WellStore *store);
void *well_model_get(unsigned int type_id, long long oid);
void *well_model_peek(unsigned int type_id, long long oid);
void *well_model_get_ctx(unsigned int type_id, long long oid, WellCtx *ctx);
void *well_model_peek_ctx(unsigned int type_id, long long oid, WellCtx *ctx);
void *well_model_find_by_oid(unsigned int type_id, long long oid);
void well_model_set(void *obj, WellCtx *ctx);
void well_model_remove(unsigned int type_id, long long oid, WellCtx *ctx);

/* Strings */
WellString *well_string_lit(const char *s);
WellString *well_string_new(const char *s);
WellString *well_string_concat(WellString *a, WellString *b);
const char *well_string_cstr(WellString *s);
int well_string_eq(WellString *a, WellString *b);
long long well_string_len(WellString *s);

/* String helpers, all byte oriented (consistent with String.length). */
long long well_str_byte_at(WellString *s, long long i); /* -1 when out of range */
WellString *well_str_from_byte(int code);               /* low 8 bits */
WellString *well_str_substring(WellString *s, long long start, long long end);
long long well_str_index_of(WellString *s, WellString *needle);
long long well_str_index_of_from(WellString *s, WellString *needle, long long from);
int well_str_contains(WellString *s, WellString *needle);
int well_str_starts_with(WellString *s, WellString *prefix);
int well_str_ends_with(WellString *s, WellString *suffix);
WellString *well_str_trim(WellString *s);
WellArray *well_str_split(WellString *s, WellString *sep, WellCtx *ctx);
WellString *well_str_to_lower(WellString *s);
WellString *well_str_to_upper(WellString *s);
WellString *well_str_from_int(long long v);

/* Bytes: mutable, byte addressed. get(byteAt) returns -1 when out of range. */
WellBytes *well_bytes_new(WellCtx *ctx);
long long well_bytes_len(WellBytes *b);
void well_bytes_push(WellBytes *b, int byte, WellCtx *ctx);
void well_bytes_append(WellBytes *b, WellBytes *src, WellCtx *ctx);
void well_bytes_append_str(WellBytes *b, WellString *s, WellCtx *ctx);
long long well_bytes_get(WellBytes *b, long long i);
void well_bytes_set(WellBytes *b, long long i, int byte, WellCtx *ctx);
void well_bytes_clear(WellBytes *b, WellCtx *ctx);
WellString *well_bytes_to_string(WellBytes *b);

/* "take" variants: release the owned temporary after computing (inline use of
 * an owned expression such as `s.trim().length`). */
long long well_str_len_take(WellString *s);
long long well_str_byte_at_take(WellString *s, long long i);
long long well_bytes_len_take(WellBytes *b);
long long well_bytes_get_take(WellBytes *b, long long i);
long long well_array_len_take(WellArray *a);
int well_str_eq_take(WellString *a, int a_owned, WellString *b, int b_owned);
WellString *well_string_concat_take(WellString *a, int a_owned, WellString *b, int b_owned);
WellString *well_str_substring_take(WellString *s, long long start, long long end);
WellString *well_str_trim_take(WellString *s);
WellArray *well_str_split_take(WellString *s, WellString *sep, WellCtx *ctx);
long long well_str_index_of_take(WellString *s, WellString *needle);
long long well_str_index_of_from_take(WellString *s, WellString *needle, long long from);
int well_str_contains_take(WellString *s, WellString *needle);
int well_str_starts_with_take(WellString *s, WellString *prefix);
int well_str_ends_with_take(WellString *s, WellString *suffix);
WellString *well_str_to_lower_take(WellString *s);
WellString *well_str_to_upper_take(WellString *s);
WellString *well_str_replace(WellString *s, WellString *old_s, WellString *new_s);
WellString *well_str_replace_take(WellString *s, WellString *old_s, WellString *new_s);

/* containers: Array<T> / Map<T> (homogeneous, String keys) */
WellArray *well_array_new(WellCtx *ctx, int is_ref);
WellMap *well_map_new(WellCtx *ctx, int is_ref);
long long well_array_len(WellArray *a);
void well_array_push(WellArray *a, WellValue v, WellCtx *ctx);
WellValue well_array_pop(WellArray *a, WellCtx *ctx);
WellValue well_array_get(WellArray *a, long long i);
void well_array_set(WellArray *a, long long i, WellValue v, WellCtx *ctx);
WellArray *well_array_slice(WellArray *a, long long start, long long end, WellCtx *ctx);
WellArray *well_array_concat(WellArray *a, WellArray *b, WellCtx *ctx);
WellString *well_array_join(WellArray *a, WellString *sep);
long long well_array_index_of(WellArray *a, WellValue v, int kind);
int well_array_includes(WellArray *a, WellValue v, int kind);
void well_array_clear(WellArray *a, WellCtx *ctx);
/* sort kind: WELL_FIELD_INT / LONG / DOUBLE / STRING */
void well_array_sort(WellArray *a, int kind);
void well_array_reverse(WellArray *a);
WellValue well_array_shift(WellArray *a, WellCtx *ctx);
void well_array_unshift(WellArray *a, WellValue v, WellCtx *ctx);
/* take variants (owned receiver) */
WellArray *well_array_slice_take(WellArray *a, long long start, long long end, WellCtx *ctx);
WellArray *well_array_concat_take(WellArray *a, WellArray *b, WellCtx *ctx);
WellString *well_array_join_take(WellArray *a, WellString *sep);
long long well_array_index_of_take(WellArray *a, WellValue v, int kind);
int well_array_includes_take(WellArray *a, WellValue v, int kind);
WellValue well_array_get_take(WellArray *a, long long i);
long long well_map_len(WellMap *m);
void well_map_set(WellMap *m, WellString *k, WellValue v, WellCtx *ctx);
WellValue well_map_get(WellMap *m, WellString *k);
int well_map_has(WellMap *m, WellString *k);
void well_map_delete(WellMap *m, WellString *k, WellCtx *ctx);
WellString *well_map_key_at(WellMap *m, long long i);
WellValue well_map_value_at(WellMap *m, long long i);
WellArray *well_map_keys(WellMap *m, WellCtx *ctx);
WellArray *well_map_values(WellMap *m, WellCtx *ctx);
void well_map_clear(WellMap *m, WellCtx *ctx);

/* Linked<T>: doubly linked list; head/tail O(1), get O(n) */
WellLinked *well_linked_new(WellCtx *ctx, int is_ref);
long long well_linked_len(WellLinked *l);
void well_linked_push_head(WellLinked *l, WellValue v, WellCtx *ctx);
void well_linked_push_tail(WellLinked *l, WellValue v, WellCtx *ctx);
WellValue well_linked_pop_head(WellLinked *l, WellCtx *ctx);
WellValue well_linked_pop_tail(WellLinked *l, WellCtx *ctx);
WellValue well_linked_head(WellLinked *l);
WellValue well_linked_tail(WellLinked *l);
WellValue well_linked_get(WellLinked *l, long long i);
WellLinkedNode *well_linked_first(WellLinked *l);
WellLinkedNode *well_linked_next(WellLinkedNode *n);
WellValue well_linked_value(WellLinkedNode *n);

static inline WellValue well_box_i(long long v) { WellValue x; x.i = v; return x; }
static inline WellValue well_box_d(double v) { WellValue x; x.d = v; return x; }
static inline WellValue well_box_p(void *v) { WellValue x; x.p = v; return x; }
static inline long long well_unbox_i(WellValue v) { return v.i; }
static inline double well_unbox_d(WellValue v) { return v.d; }
static inline void *well_unbox_p(WellValue v) { return v.p; }

/* events / subscriptions */
#define WELL_CHANNEL_ALL (-1LL)
typedef void (*WellHandlerFn)(WellCtx *, void *);
void well_on_type(unsigned int event_type, WellHandlerFn fn, int once, long long channel);
void well_off_type(unsigned int event_type, WellHandlerFn fn, long long channel);
void well_emit_event(void *event, long long channel);

typedef void (*WellTaskFn)(WellCtx *ctx, void *arg);
typedef void (*WellTaskDone)(void *arg);
void well_schedule_task(long long oid, WellTaskFn fn, void *arg, WellTaskDone done);

/* Timers: fire a task after `ms`; info carries id/interval/fires. */
typedef struct WellTimerInfo {
    long long id;
    long long interval;
    long long fires;
} WellTimerInfo;
typedef void (*WellTimerFn)(WellCtx *ctx, void *info);
long long well_timer_once(long long ms, WellTimerFn fn);
long long well_timer_loop(long long ms, WellTimerFn fn);
void well_timer_cancel(long long id);
void well_timer_shutdown(void);

void well_run_loop(void);
void well_owner_push(int owner);
void well_owner_pop(void);
int well_owner_current(void);
void well_off_owner(int owner);

/* print */
void well_print_begin(void);
void well_print_end(void);
void well_print_str(WellString *s);
void well_print_int(long long v);
void well_print_bool(int v);
void well_print_double(double v);
void well_print_nl(void);

/* IdUtil.getId(): long (serverId + timestamp + sequence) */
long long well_id_util_get_id(void);

/* File API */
WellString *well_file_read(const char *path);
void well_file_write(const char *path, const char *text);
int well_file_exists(const char *path);
void well_file_remove(const char *path);

/* Host API */
long long well_host_now_ms(void);
WellString *well_host_env(const char *name);
void well_log_str(WellString *s);
void well_log_int(long long v);
void well_log_bool(int v);
void well_log_double(double v);
void well_log_nl(void);

#endif
