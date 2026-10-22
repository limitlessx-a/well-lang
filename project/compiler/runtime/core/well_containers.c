#include "wellrt.h"
#include "well_internal.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <setjmp.h>

/* ---------- containers ---------- */
struct WellArray {
    WellObj hdr;
    WellValue *items;
    long long len;
    long long cap;
    int is_ref;
};

struct WellMap {
    WellObj hdr;
    WellString **keys;   /* insertion order */
    WellValue *vals;
    long long len;
    long long cap;
    long long *slots;    /* 0 = empty, -1 = tombstone, >0 = entry index + 1 */
    long long slot_cap;  /* power of two */
    long long tomb;
    int is_ref;
};

#define MAP_TOMBSTONE (-1LL)
#define MAP_LOAD_NUM 7
#define MAP_LOAD_DEN 10

static void container_check_ref(void *owner, void *elem) {
    WellObj *o = (WellObj *)owner;
    WellObj *e = (WellObj *)elem;
    if (!elem) return;
    if (e->type_id != WELL_T_STRING && e->oid != o->oid)
        well_panic("container element oid mismatch");
}

static void container_release(WellValue v, int is_ref) {
    if (is_ref && v.p) well_release(v.p);
}

WellArray *well_array_new(WellCtx *ctx, int is_ref) {
    WellArray *a = (WellArray *)well_new(sizeof(WellArray), WELL_T_ARRAY, ctx ? ctx->oid : 0);
    a->items = NULL;
    a->len = 0;
    a->cap = 0;
    a->is_ref = is_ref;
    return a;
}

WellMap *well_map_new(WellCtx *ctx, int is_ref) {
    WellMap *m = (WellMap *)well_new(sizeof(WellMap), WELL_T_MAP, ctx ? ctx->oid : 0);
    m->keys = NULL;
    m->vals = NULL;
    m->len = 0;
    m->cap = 0;
    m->slots = NULL;
    m->slot_cap = 0;
    m->tomb = 0;
    m->is_ref = is_ref;
    return m;
}

long long well_array_len(WellArray *a) { return a ? a->len : 0; }

static void array_grow(WellArray *a, long long need) {
    if (need <= a->cap) return;
    long long ncap = a->cap ? a->cap * 2 : 8;
    while (ncap < need) ncap *= 2;
    WellValue *ni = (WellValue *)realloc(a->items, (size_t)ncap * sizeof(WellValue));
    if (!ni) well_panic("out of memory (array)");
    a->items = ni;
    a->cap = ncap;
}

void well_array_push(WellArray *a, WellValue v, WellCtx *ctx) {
    if (!a) well_panic("push on null array");
    well_check_oid((void *)a, ctx, "container write");
    if (a->is_ref && v.p) {
        container_check_ref(a, v.p);
        well_graph_add_edge((WellObj *)a, (WellObj *)v.p);
    }
    array_grow(a, a->len + 1);
    a->items[a->len++] = v;
    if (a->is_ref && v.p) well_retain(v.p);
}

WellValue well_array_pop(WellArray *a, WellCtx *ctx) {
    WellValue v;
    v.i = 0;
    if (!a) return v;
    well_check_oid((void *)a, ctx, "container write");
    if (a->len == 0) return v;
    v = a->items[--a->len];
    if (a->is_ref && v.p)
        well_graph_remove_edge((WellObj *)a, (WellObj *)v.p);
    return v; /* ownership transferred to caller for refs */
}

WellValue well_array_get(WellArray *a, long long i) {
    WellValue v;
    v.i = 0;
    if (!a || i < 0 || i >= a->len) return v;
    return a->items[i];
}

void well_array_set(WellArray *a, long long i, WellValue v, WellCtx *ctx) {
    if (!a) well_panic("set on null array");
    well_check_oid((void *)a, ctx, "container write");
    if (i < 0) well_panic("array index out of range");
    if (i >= a->len) {
        array_grow(a, i + 1);
        while (a->len <= i) {
            WellValue z;
            z.i = 0;
            a->items[a->len++] = z;
        }
    }
    if (a->is_ref && v.p) {
        container_check_ref(a, v.p);
        well_graph_add_edge((WellObj *)a, (WellObj *)v.p);
    }
    if (a->is_ref && a->items[i].p) {
        well_graph_remove_edge((WellObj *)a, (WellObj *)a->items[i].p);
        well_release(a->items[i].p);
    }
    a->items[i] = v;
    if (a->is_ref && v.p) well_retain(v.p);
}

/* ---------- Array extra methods ---------- */

WellArray *well_array_slice(WellArray *a, long long start, long long end, WellCtx *ctx) {
    WellArray *r = well_array_new(ctx, a ? a->is_ref : 0);
    long long n = a ? a->len : 0, i;
    if (start < 0) start = 0;
    if (end > n) end = n;
    if (start > end) start = end;
    for (i = start; i < end; i++) well_array_push(r, a->items[i], ctx);
    return r;
}

WellArray *well_array_concat(WellArray *a, WellArray *b, WellCtx *ctx) {
    WellArray *r = well_array_new(ctx, a ? a->is_ref : (b ? b->is_ref : 0));
    long long i;
    if (a) for (i = 0; i < a->len; i++) well_array_push(r, a->items[i], ctx);
    if (b) for (i = 0; i < b->len; i++) well_array_push(r, b->items[i], ctx);
    return r;
}

/* Array<String> only (checked by the compiler). */
WellString *well_array_join(WellArray *a, WellString *sep) {
    const char *sp = sep ? sep->data : "";
    size_t spl = strlen(sp);
    long long n = a ? a->len : 0, i;
    size_t total = 0;
    char *buf, *p;
    WellString *out;
    for (i = 0; i < n; i++) {
        WellString *s = (WellString *)a->items[i].p;
        total += (size_t)(s ? s->len : 0);
        if (i) total += spl;
    }
    buf = (char *)malloc(total + 1);
    if (!buf) well_panic("out of memory (array join)");
    p = buf;
    for (i = 0; i < n; i++) {
        WellString *s = (WellString *)a->items[i].p;
        const char *d = s ? s->data : "";
        size_t l = (size_t)(s ? s->len : 0);
        if (i) { memcpy(p, sp, spl); p += spl; }
        memcpy(p, d, l);
        p += l;
    }
    *p = 0;
    out = well_string_new(buf);
    free(buf);
    return out;
}

/* kind: 0 int/long/bool, 1 double/float, 2 pointer identity, 3 String content */
long long well_array_index_of(WellArray *a, WellValue v, int kind) {
    long long i;
    if (!a) return -1;
    for (i = 0; i < a->len; i++) {
        switch (kind) {
        case 0: if (a->items[i].i == v.i) return i; break;
        case 1: if (a->items[i].d == v.d) return i; break;
        case 3: if (well_string_eq((WellString *)a->items[i].p, (WellString *)v.p)) return i; break;
        default: if (a->items[i].p == v.p) return i; break;
        }
    }
    return -1;
}

int well_array_includes(WellArray *a, WellValue v, int kind) {
    return well_array_index_of(a, v, kind) >= 0;
}

/* Comparison kind: WELL_FIELD_INT / LONG / DOUBLE / STRING (set by codegen). */
static int array_cmp_kind(WellValue x, WellValue y, int kind) {
    switch (kind) {
    case WELL_FIELD_INT: {
        int a = (int)x.i, b = (int)y.i;
        return (a > b) - (a < b);
    }
    case WELL_FIELD_LONG: {
        long long a = x.i, b = y.i;
        return (a > b) - (a < b);
    }
    case WELL_FIELD_DOUBLE: {
        double a = x.d, b = y.d;
        return (a > b) - (a < b);
    }
    case WELL_FIELD_STRING: {
        WellString *a = (WellString *)x.p, *b = (WellString *)y.p;
        long long la = a ? a->len : 0, lb = b ? b->len : 0;
        long long n = la < lb ? la : lb;
        int c = n ? memcmp(a->data, b->data, (size_t)n) : 0;
        if (c) return c < 0 ? -1 : 1;
        return (la > lb) - (la < lb);
    }
    default:
        return 0;
    }
}

/* In-place ascending sort (insertion sort; keeps element refcounts untouched). */
void well_array_sort(WellArray *a, int kind, WellCtx *ctx) {
    long long n, i, j;
    if (!a) well_panic("sort on null array");
    well_check_oid((void *)a, ctx, "Array.sort");
    if (a->len < 2) return;
    n = a->len;
    for (i = 1; i < n; i++) {
        WellValue v = a->items[i];
        j = i - 1;
        while (j >= 0 && array_cmp_kind(a->items[j], v, kind) > 0) {
            a->items[j + 1] = a->items[j];
            j--;
        }
        a->items[j + 1] = v;
    }
}

void well_array_reverse(WellArray *a, WellCtx *ctx) {
    long long i, j;
    if (!a) well_panic("reverse on null array");
    well_check_oid((void *)a, ctx, "Array.reverse");
    if (a->len < 2) return;
    for (i = 0, j = a->len - 1; i < j; i++, j--) {
        WellValue t = a->items[i];
        a->items[i] = a->items[j];
        a->items[j] = t;
    }
}

void well_array_clear(WellArray *a, WellCtx *ctx) {
    long long i;
    if (!a) return;
    well_check_oid((void *)a, ctx, "container write");
    if (a->is_ref) {
        for (i = 0; i < a->len; i++) {
            if (a->items[i].p) {
                well_graph_remove_edge((WellObj *)a, (WellObj *)a->items[i].p);
                well_release(a->items[i].p);
            }
        }
    }
    a->len = 0;
}

WellValue well_array_shift(WellArray *a, WellCtx *ctx) {
    WellValue v;
    long long i;
    v.i = 0;
    if (!a) return v;
    well_check_oid((void *)a, ctx, "container write");
    if (a->len == 0) return v;
    v = a->items[0];
    if (a->is_ref && v.p)
        well_graph_remove_edge((WellObj *)a, (WellObj *)v.p);
    for (i = 1; i < a->len; i++) a->items[i - 1] = a->items[i];
    a->len--;
    return v;
}

void well_array_unshift(WellArray *a, WellValue v, WellCtx *ctx) {
    long long i;
    if (!a) well_panic("unshift on null array");
    well_check_oid((void *)a, ctx, "container write");
    if (a->is_ref && v.p) {
        container_check_ref(a, v.p);
        well_graph_add_edge((WellObj *)a, (WellObj *)v.p);
    }
    array_grow(a, a->len + 1);
    for (i = a->len; i > 0; i--) a->items[i] = a->items[i - 1];
    a->len++;
    a->items[0] = v;
    if (a->is_ref && v.p) well_retain(v.p);
}

/* ---------- take variants for owned receivers ---------- */

WellArray *well_array_slice_take(WellArray *a, long long start, long long end, WellCtx *ctx) {
    WellArray *r = well_array_slice(a, start, end, ctx);
    well_release(a);
    return r;
}

WellArray *well_array_concat_take(WellArray *a, WellArray *b, WellCtx *ctx) {
    WellArray *r = well_array_concat(a, b, ctx);
    well_release(a);
    return r;
}

WellString *well_array_join_take(WellArray *a, WellString *sep) {
    WellString *r = well_array_join(a, sep);
    well_release(a);
    return r;
}

long long well_array_index_of_take(WellArray *a, WellValue v, int kind) {
    long long i = well_array_index_of(a, v, kind);
    well_release(a);
    return i;
}

int well_array_includes_take(WellArray *a, WellValue v, int kind) {
    int r = well_array_includes(a, v, kind);
    well_release(a);
    return r;
}

WellValue well_array_get_take(WellArray *a, long long i) {
    WellValue v = well_array_get(a, i);
    if (a && a->is_ref && v.p) well_retain(v.p);
    well_release(a);
    return v;
}

long long well_map_len(WellMap *m) { return m ? m->len : 0; }

/* ---------- Map extra methods ---------- */

static void map_rehash(WellMap *m, long long ncap);

WellArray *well_map_keys(WellMap *m, WellCtx *ctx) {
    WellArray *r = well_array_new(ctx, 1);
    long long i;
    WellValue v;
    if (!m) return r;
    for (i = 0; i < m->len; i++) {
        v.p = m->keys[i];
        well_array_push(r, v, ctx);
    }
    return r;
}

WellArray *well_map_values(WellMap *m, WellCtx *ctx) {
    WellArray *r = well_array_new(ctx, m ? m->is_ref : 0);
    long long i;
    if (!m) return r;
    for (i = 0; i < m->len; i++) well_array_push(r, m->vals[i], ctx);
    return r;
}

void well_map_clear(WellMap *m, WellCtx *ctx) {
    long long i;
    if (!m) return;
    well_check_oid((void *)m, ctx, "container write");
    for (i = 0; i < m->len; i++) {
        well_release(m->keys[i]);
        if (m->is_ref && m->vals[i].p) {
            well_graph_remove_edge((WellObj *)m, (WellObj *)m->vals[i].p);
            well_release(m->vals[i].p);
        }
    }
    m->len = 0;
    m->tomb = 0;
    if (m->slot_cap) map_rehash(m, m->slot_cap);
}

static void map_rehash(WellMap *m, long long ncap) {
    long long *ns;
    long long mask;
    long long i;
    if (ncap < 8) ncap = 8;
    ns = (long long *)calloc((size_t)ncap, sizeof(long long));
    if (!ns) well_panic("out of memory (map hash)");
    mask = ncap - 1;
    for (i = 0; i < m->len; i++) {
        unsigned long long h = m->keys[i]->hash & (unsigned long long)mask;
        while (ns[h]) h = (h + 1) & (unsigned long long)mask;
        ns[h] = i + 1;
    }
    free(m->slots);
    m->slots = ns;
    m->slot_cap = ncap;
    m->tomb = 0;
}

static void map_grow_entries(WellMap *m, long long need) {
    if (need <= m->cap) return;
    long long ncap = m->cap ? m->cap * 2 : 8;
    while (ncap < need) ncap *= 2;
    WellString **nk = (WellString **)realloc(m->keys, (size_t)ncap * sizeof(WellString *));
    WellValue *nv = (WellValue *)realloc(m->vals, (size_t)ncap * sizeof(WellValue));
    if (!nk || !nv) well_panic("out of memory (map)");
    m->keys = nk;
    m->vals = nv;
    m->cap = ncap;
}

static void map_ensure(WellMap *m, long long need) {
    map_grow_entries(m, need);
    if (m->slot_cap == 0 ||
        (need + m->tomb) * MAP_LOAD_DEN >= m->slot_cap * MAP_LOAD_NUM) {
        long long ncap = 8;
        while (ncap * MAP_LOAD_NUM < (need + m->tomb) * MAP_LOAD_DEN) ncap *= 2;
        map_rehash(m, ncap);
    }
}

/* Returns the slot whose entry matches k, or -1. */
static long long map_find_slot(WellMap *m, WellString *k) {
    unsigned long long mask, h;
    if (!m || !k || m->slot_cap == 0) return -1;
    mask = (unsigned long long)(m->slot_cap - 1);
    h = k->hash & mask;
    for (;;) {
        long long s = m->slots[h];
        if (s == 0) return -1;
        if (s > 0 && well_string_eq(m->keys[s - 1], k)) return (long long)h;
        h = (h + 1) & mask;
    }
}

static void map_insert_slot(WellMap *m, long long entry) {
    unsigned long long mask = (unsigned long long)(m->slot_cap - 1);
    unsigned long long h = m->keys[entry]->hash & mask;
    long long tomb = -1;
    for (;;) {
        long long s = m->slots[h];
        if (s == 0) {
            if (tomb >= 0) h = (unsigned long long)tomb;
            m->slots[h] = entry + 1;
            return;
        }
        if (s == MAP_TOMBSTONE && tomb < 0) tomb = (long long)h;
        h = (h + 1) & mask;
    }
}

void well_map_set(WellMap *m, WellString *k, WellValue v, WellCtx *ctx) {
    long long slot;
    if (!m) well_panic("set on null map");
    if (!k) well_panic("Map key cannot be null");
    well_check_oid((void *)m, ctx, "container write");
    if (m->is_ref && v.p) {
        container_check_ref(m, v.p);
        well_graph_add_edge((WellObj *)m, (WellObj *)v.p);
    }
    map_ensure(m, m->len + 1);
    slot = map_find_slot(m, k);
    if (slot >= 0) {
        long long i = m->slots[slot] - 1;
        if (m->is_ref && m->vals[i].p) {
            well_graph_remove_edge((WellObj *)m, (WellObj *)m->vals[i].p);
            well_release(m->vals[i].p);
        }
        m->vals[i] = v;
        if (m->is_ref && v.p) well_retain(v.p);
        return;
    }
    m->keys[m->len] = k;
    m->vals[m->len] = v;
    map_insert_slot(m, m->len);
    m->len++;
    well_retain(k);
    if (m->is_ref && v.p) well_retain(v.p);
}

WellValue well_map_get(WellMap *m, WellString *k) {
    WellValue v;
    v.i = 0;
    if (!m || !k) return v;
    {
        long long slot = map_find_slot(m, k);
        if (slot >= 0) return m->vals[m->slots[slot] - 1];
    }
    return v;
}

int well_map_has(WellMap *m, WellString *k) {
    return m && k && map_find_slot(m, k) >= 0;
}

void well_map_delete(WellMap *m, WellString *k, WellCtx *ctx) {
    long long slot, i;
    if (!m || !k) return;
    well_check_oid((void *)m, ctx, "container write");
    slot = map_find_slot(m, k);
    if (slot < 0) return;
    i = m->slots[slot] - 1;
    well_release(m->keys[i]);
    if (m->is_ref && m->vals[i].p) {
        well_graph_remove_edge((WellObj *)m, (WellObj *)m->vals[i].p);
        well_release(m->vals[i].p);
    }
    for (; i + 1 < m->len; i++) {
        m->keys[i] = m->keys[i + 1];
        m->vals[i] = m->vals[i + 1];
    }
    m->len--;
    m->tomb++;
    /* Rebuild slots after the ordered arrays shifted. */
    if (m->slot_cap) map_rehash(m, m->slot_cap);
}

WellString *well_map_key_at(WellMap *m, long long i) {
    if (!m || i < 0 || i >= m->len) return NULL;
    return m->keys[i];
}

WellValue well_map_value_at(WellMap *m, long long i) {
    WellValue v;
    v.i = 0;
    if (!m || i < 0 || i >= m->len) return v;
    return m->vals[i];
}

static void array_destroy(void *p) {
    WellArray *a = (WellArray *)p;
    long long i;
    if (!a) return;
    if (a->is_ref) {
        for (i = 0; i < a->len; i++) container_release(a->items[i], 1);
    }
    free(a->items);
    free(a);
}

static void map_destroy(void *p) {
    WellMap *m = (WellMap *)p;
    long long i;
    if (!m) return;
    for (i = 0; i < m->len; i++) {
        well_release(m->keys[i]);
        if (m->is_ref) container_release(m->vals[i], 1);
    }
    free(m->keys);
    free(m->vals);
    free(m->slots);
    free(m);
}

/* ---------- Linked<T> ---------- */
struct WellLinkedNode {
    WellValue v;
    struct WellLinkedNode *prev;
    struct WellLinkedNode *next;
};

struct WellLinked {
    WellObj hdr;
    WellLinkedNode *head;
    WellLinkedNode *tail;
    long long len;
    int is_ref;
};

WellLinked *well_linked_new(WellCtx *ctx, int is_ref) {
    WellLinked *l = (WellLinked *)well_new(sizeof(WellLinked), WELL_T_LINKED, ctx ? ctx->oid : 0);
    l->head = NULL;
    l->tail = NULL;
    l->len = 0;
    l->is_ref = is_ref;
    return l;
}

long long well_linked_len(WellLinked *l) { return l ? l->len : 0; }

static WellLinkedNode *linked_node_new(WellValue v) {
    WellLinkedNode *n = (WellLinkedNode *)malloc(sizeof(WellLinkedNode));
    if (!n) well_panic("out of memory (Linked)");
    n->v = v;
    n->prev = NULL;
    n->next = NULL;
    return n;
}

static void linked_link_head(WellLinked *l, WellLinkedNode *n) {
    n->prev = NULL;
    n->next = l->head;
    if (l->head) l->head->prev = n;
    else l->tail = n;
    l->head = n;
    l->len++;
}

static void linked_link_tail(WellLinked *l, WellLinkedNode *n) {
    n->next = NULL;
    n->prev = l->tail;
    if (l->tail) l->tail->next = n;
    else l->head = n;
    l->tail = n;
    l->len++;
}

void well_linked_push_head(WellLinked *l, WellValue v, WellCtx *ctx) {
    WellLinkedNode *n;
    if (!l) well_panic("pushHead on null Linked");
    well_check_oid((void *)l, ctx, "container write");
    if (l->is_ref && v.p) {
        container_check_ref(l, v.p);
        well_graph_add_edge((WellObj *)l, (WellObj *)v.p);
    }
    n = linked_node_new(v);
    linked_link_head(l, n);
    if (l->is_ref && v.p) well_retain(v.p);
}

void well_linked_push_tail(WellLinked *l, WellValue v, WellCtx *ctx) {
    WellLinkedNode *n;
    if (!l) well_panic("pushTail on null Linked");
    well_check_oid((void *)l, ctx, "container write");
    if (l->is_ref && v.p) {
        container_check_ref(l, v.p);
        well_graph_add_edge((WellObj *)l, (WellObj *)v.p);
    }
    n = linked_node_new(v);
    linked_link_tail(l, n);
    if (l->is_ref && v.p) well_retain(v.p);
}

WellValue well_linked_pop_head(WellLinked *l, WellCtx *ctx) {
    WellLinkedNode *n;
    WellValue v;
    v.i = 0;
    if (!l) return v;
    well_check_oid((void *)l, ctx, "container write");
    n = l->head;
    if (!n) return v;
    l->head = n->next;
    if (l->head) l->head->prev = NULL;
    else l->tail = NULL;
    l->len--;
    v = n->v;
    if (l->is_ref && v.p)
        well_graph_remove_edge((WellObj *)l, (WellObj *)v.p);
    free(n);
    return v; /* ownership transferred */
}

WellValue well_linked_pop_tail(WellLinked *l, WellCtx *ctx) {
    WellLinkedNode *n;
    WellValue v;
    v.i = 0;
    if (!l) return v;
    well_check_oid((void *)l, ctx, "container write");
    n = l->tail;
    if (!n) return v;
    l->tail = n->prev;
    if (l->tail) l->tail->next = NULL;
    else l->head = NULL;
    l->len--;
    v = n->v;
    if (l->is_ref && v.p)
        well_graph_remove_edge((WellObj *)l, (WellObj *)v.p);
    free(n);
    return v;
}

WellValue well_linked_head(WellLinked *l) {
    WellValue v;
    v.i = 0;
    if (!l || !l->head) return v;
    return l->head->v;
}

WellValue well_linked_tail(WellLinked *l) {
    WellValue v;
    v.i = 0;
    if (!l || !l->tail) return v;
    return l->tail->v;
}

WellValue well_linked_get(WellLinked *l, long long i) {
    WellLinkedNode *n;
    WellValue v;
    v.i = 0;
    if (!l || i < 0 || i >= l->len) return v;
    if (i < l->len / 2) {
        n = l->head;
        while (i-- > 0) n = n->next;
    } else {
        n = l->tail;
        i = l->len - 1 - i;
        while (i-- > 0) n = n->prev;
    }
    return n->v;
}

WellLinkedNode *well_linked_first(WellLinked *l) { return l ? l->head : NULL; }
WellLinkedNode *well_linked_next(WellLinkedNode *n) { return n ? n->next : NULL; }
WellValue well_linked_value(WellLinkedNode *n) {
    WellValue v;
    v.i = 0;
    return n ? n->v : v;
}

static void linked_destroy(void *p) {
    WellLinked *l = (WellLinked *)p;
    WellLinkedNode *n, *next;
    if (!l) return;
    n = l->head;
    while (n) {
        next = n->next;
        if (l->is_ref && n->v.p) well_release(n->v.p);
        free(n);
        n = next;
    }
    free(l);
}

void well_array_force_clear(void *p) {
    WellArray *a = (WellArray *)p;
    long long i;
    if (!a) return;
    if (a->is_ref && a->items) {
        for (i = 0; i < a->len; i++) {
            WellObj *o = (WellObj *)a->items[i].p;
            if (o && o->type_id == WELL_T_STRING) well_release(o);
        }
    }
    free(a->items);
    a->items = NULL;
    a->len = 0;
    a->cap = 0;
}

void well_map_force_clear(void *p) {
    WellMap *m = (WellMap *)p;
    long long i;
    if (!m) return;
    for (i = 0; i < m->len; i++) {
        if (m->keys && m->keys[i]) well_release(m->keys[i]);
        if (m->is_ref && m->vals) {
            WellObj *o = (WellObj *)m->vals[i].p;
            if (o && o->type_id == WELL_T_STRING) well_release(o);
        }
    }
    free(m->keys); free(m->vals); free(m->slots);
    m->keys = NULL; m->vals = NULL; m->slots = NULL;
    m->len = 0; m->cap = 0; m->slot_cap = 0; m->tomb = 0;
}

void well_linked_force_clear(void *p) {
    WellLinked *l = (WellLinked *)p;
    WellLinkedNode *n, *next;
    if (!l) return;
    n = l->head;
    while (n) {
        next = n->next;
        if (l->is_ref && n->v.p) {
            WellObj *o = (WellObj *)n->v.p;
            if (o && o->type_id == WELL_T_STRING) well_release(o);
        }
        free(n);
        n = next;
    }
    l->head = NULL; l->tail = NULL; l->len = 0;
}

void well_containers_register(void) {
    well_register_type(WELL_T_ARRAY, array_destroy);
    well_register_type(WELL_T_MAP, map_destroy);
    well_register_type(WELL_T_LINKED, linked_destroy);
}

