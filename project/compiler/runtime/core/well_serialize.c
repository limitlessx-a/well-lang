#include "wellrt.h"
#include "well_internal.h"

#include <stdlib.h>
#include <string.h>

#define WELL_SER_MAGIC 0x31534C57u /* "WLS1" little-endian */
#define WELL_SER_VERSION 1

typedef struct {
    unsigned char *buf;
    size_t len;
    size_t cap;
} SerBuf;

typedef struct {
    const unsigned char *buf;
    size_t len;
    size_t pos;
} SerCur;

static void sb_need(SerBuf *b, size_t n) {
    if (b->len + n > b->cap) {
        size_t ncap = b->cap ? b->cap * 2 : 256;
        unsigned char *nb;
        while (ncap < b->len + n) ncap *= 2;
        nb = (unsigned char *)realloc(b->buf, ncap);
        if (!nb) well_panic("out of memory (encode)");
        b->buf = nb;
        b->cap = ncap;
    }
}

static void sb_u8(SerBuf *b, unsigned v) {
    sb_need(b, 1);
    b->buf[b->len++] = (unsigned char)(v & 0xFFu);
}

static void sb_u32(SerBuf *b, unsigned v) {
    sb_need(b, 4);
    b->buf[b->len++] = (unsigned char)(v & 0xFFu);
    b->buf[b->len++] = (unsigned char)((v >> 8) & 0xFFu);
    b->buf[b->len++] = (unsigned char)((v >> 16) & 0xFFu);
    b->buf[b->len++] = (unsigned char)((v >> 24) & 0xFFu);
}

static void sb_u64(SerBuf *b, unsigned long long v) {
    int i;
    sb_need(b, 8);
    for (i = 0; i < 8; i++) b->buf[b->len++] = (unsigned char)((v >> (8 * i)) & 0xFFu);
}

static void sb_bytes(SerBuf *b, const void *p, size_t n) {
    sb_need(b, n);
    memcpy(b->buf + b->len, p, n);
    b->len += n;
}

static unsigned rd_u8(SerCur *c) {
    if (c->pos + 1 > c->len) well_panic("bad serialization: truncated");
    return c->buf[c->pos++];
}

static unsigned rd_u32(SerCur *c) {
    unsigned v;
    if (c->pos + 4 > c->len) well_panic("bad serialization: truncated");
    v = (unsigned)c->buf[c->pos] |
        ((unsigned)c->buf[c->pos + 1] << 8) |
        ((unsigned)c->buf[c->pos + 2] << 16) |
        ((unsigned)c->buf[c->pos + 3] << 24);
    c->pos += 4;
    return v;
}

static unsigned long long rd_u64(SerCur *c) {
    unsigned long long v = 0;
    int i;
    if (c->pos + 8 > c->len) well_panic("bad serialization: truncated");
    for (i = 0; i < 8; i++) v |= ((unsigned long long)c->buf[c->pos + i]) << (8 * i);
    c->pos += 8;
    return v;
}

static void rd_bytes(SerCur *c, void *p, size_t n) {
    if (c->pos + n > c->len) well_panic("bad serialization: truncated");
    memcpy(p, c->buf + c->pos, n);
    c->pos += n;
}

static void ser_string(SerBuf *b, WellString *s) {
    if (!s) {
        sb_u8(b, 0);
        return;
    }
    sb_u8(b, 1);
    sb_u32(b, (unsigned)well_string_len(s));
    sb_bytes(b, well_string_cstr(s), (size_t)well_string_len(s));
}

static WellString *deser_string(SerCur *c) {
    unsigned present = rd_u8(c);
    unsigned len;
    char *tmp;
    WellString *s;
    if (!present) return NULL;
    len = rd_u32(c);
    tmp = (char *)malloc((size_t)len + 1);
    if (!tmp) well_panic("out of memory (decode string)");
    rd_bytes(c, tmp, len);
    tmp[len] = '\0';
    s = well_string_new(tmp);
    free(tmp);
    return s;
}

static void ser_ref(SerBuf *b, unsigned int type_id, WellObj *o) {
    if (!o) {
        sb_u8(b, 0);
        return;
    }
    sb_u8(b, 1);
    sb_u32(b, type_id ? type_id : o->type_id);
    sb_u64(b, (unsigned long long)o->oid);
}

static void ser_value(SerBuf *b, int kind, unsigned int type_id, WellValue v) {
    switch (kind) {
    case WELL_FIELD_INT:
    case WELL_FIELD_LONG:
    case WELL_FIELD_BOOL:
        sb_u64(b, (unsigned long long)v.i);
        break;
    case WELL_FIELD_DOUBLE: {
        unsigned long long u;
        memcpy(&u, &v.d, 8);
        sb_u64(b, u);
        break;
    }
    case WELL_FIELD_STRING:
        ser_string(b, (WellString *)v.p);
        break;
    case WELL_FIELD_REF:
        ser_ref(b, type_id, (WellObj *)v.p);
        break;
    default:
        well_panic("serialize: unsupported container element kind");
    }
}

static void ser_container(SerBuf *b, int kind, int elem_kind, unsigned int elem_type_id, void *p) {
    long long i, n;
    if (!p) {
        sb_u8(b, 0);
        return;
    }
    sb_u8(b, 1);
    if (kind == WELL_FIELD_ARRAY) {
        WellArray *a = (WellArray *)p;
        n = well_array_len(a);
        sb_u32(b, (unsigned)n);
        for (i = 0; i < n; i++) ser_value(b, elem_kind, elem_type_id, well_array_get(a, i));
    } else if (kind == WELL_FIELD_MAP) {
        WellMap *m = (WellMap *)p;
        n = well_map_len(m);
        sb_u32(b, (unsigned)n);
        for (i = 0; i < n; i++) {
            ser_string(b, well_map_key_at(m, i));
            ser_value(b, elem_kind, elem_type_id, well_map_value_at(m, i));
        }
    } else if (kind == WELL_FIELD_LINKED) {
        WellLinked *l = (WellLinked *)p;
        WellLinkedNode *node;
        n = well_linked_len(l);
        sb_u32(b, (unsigned)n);
        for (node = well_linked_first(l); node; node = well_linked_next(node))
            ser_value(b, elem_kind, elem_type_id, well_linked_value(node));
    } else {
        well_panic("serialize: bad container kind");
    }
}

unsigned char *well_encode(void *obj, size_t *out_len) {
    WellObj *o = (WellObj *)obj;
    const WellTypeDesc *d;
    SerBuf b;
    int i;
    if (!o) well_panic("encode(null)");
    d = well_type_desc(o->type_id);
    if (!d) well_panic("encode: unknown type");
    memset(&b, 0, sizeof(b));
    sb_u32(&b, WELL_SER_MAGIC);
    sb_u8(&b, WELL_SER_VERSION);
    sb_u32(&b, d->type_id);
    sb_u32(&b, (unsigned)d->field_count);
    for (i = 0; i < d->field_count; i++) {
        const WellFieldDesc *f = &d->fields[i];
        void *p = (char *)obj + f->offset;
        switch (f->kind) {
        case WELL_FIELD_INT:
            sb_u64(&b, (unsigned long long)*(int *)p);
            break;
        case WELL_FIELD_LONG:
            sb_u64(&b, (unsigned long long)*(long long *)p);
            break;
        case WELL_FIELD_BOOL:
            sb_u64(&b, (unsigned long long)(*(int *)p ? 1 : 0));
            break;
        case WELL_FIELD_DOUBLE: {
            unsigned long long u;
            memcpy(&u, p, 8);
            sb_u64(&b, u);
            break;
        }
        case WELL_FIELD_STRING:
            ser_string(&b, *(WellString **)p);
            break;
        case WELL_FIELD_REF:
            ser_ref(&b, f->type_id, *(WellObj **)p);
            break;
        case WELL_FIELD_ARRAY:
        case WELL_FIELD_MAP:
        case WELL_FIELD_LINKED:
            ser_container(&b, f->kind, f->elem_kind, f->elem_type_id, *(void **)p);
            break;
        default:
            well_panic("encode: unsupported field kind");
        }
    }
    if (out_len) *out_len = b.len;
    return b.buf;
}

static WellValue deser_value(SerCur *c, int kind, unsigned int type_id,
                             WellRefResolver resolve, void *user, void **owned) {
    WellValue v;
    v.i = 0;
    if (owned) *owned = NULL;
    switch (kind) {
    case WELL_FIELD_INT:
    case WELL_FIELD_LONG:
    case WELL_FIELD_BOOL:
        v.i = (long long)rd_u64(c);
        break;
    case WELL_FIELD_DOUBLE: {
        unsigned long long u = rd_u64(c);
        memcpy(&v.d, &u, 8);
        break;
    }
    case WELL_FIELD_STRING:
        v.p = deser_string(c);
        if (owned) *owned = v.p;
        break;
    case WELL_FIELD_REF: {
        unsigned present = rd_u8(c);
        unsigned tid;
        unsigned long long oid;
        if (!present) {
            v.p = NULL;
            break;
        }
        tid = rd_u32(c);
        oid = rd_u64(c);
        v.p = resolve ? resolve(tid, (long long)oid, user) : NULL;
        break;
    }
    default:
        well_panic("decode: unsupported element kind");
    }
    (void)type_id;
    return v;
}

static void *deser_container(SerCur *c, int kind, int elem_kind, unsigned int elem_type_id,
                             long long oid, WellRefResolver resolve, void *user) {
    unsigned present = rd_u8(c);
    unsigned n, i;
    void *p;
    WellCtx ctx;
    int is_ref = (elem_kind == WELL_FIELD_STRING || elem_kind == WELL_FIELD_REF);
    if (!present) return NULL;
    n = rd_u32(c);
    well_ctx_init(&ctx, oid);
    if (kind == WELL_FIELD_ARRAY) {
        WellArray *a = well_array_new(&ctx, is_ref);
        for (i = 0; i < n; i++) {
            void *owned = NULL;
            WellValue v = deser_value(c, elem_kind, elem_type_id, resolve, user, &owned);
            well_array_push(a, v, &ctx);
            if (owned) well_release(owned);
        }
        p = a;
    } else if (kind == WELL_FIELD_MAP) {
        WellMap *m = well_map_new(&ctx, is_ref);
        for (i = 0; i < n; i++) {
            WellString *k = deser_string(c);
            void *owned = NULL;
            WellValue v = deser_value(c, elem_kind, elem_type_id, resolve, user, &owned);
            if (!k) well_panic("decode: null map key");
            well_map_set(m, k, v, &ctx);
            well_release(k);
            if (owned) well_release(owned);
        }
        p = m;
    } else if (kind == WELL_FIELD_LINKED) {
        WellLinked *l = well_linked_new(&ctx, is_ref);
        for (i = 0; i < n; i++) {
            void *owned = NULL;
            WellValue v = deser_value(c, elem_kind, elem_type_id, resolve, user, &owned);
            well_linked_push_tail(l, v, &ctx);
            if (owned) well_release(owned);
        }
        p = l;
    } else {
        well_panic("decode: bad container kind");
        p = NULL;
    }
    well_ctx_clear(&ctx);
    return p;
}

void *well_decode(unsigned int type_id, long long oid,
                  const unsigned char *buf, size_t len,
                  WellRefResolver resolve, void *user) {
    SerCur c;
    const WellTypeDesc *d;
    WellObj *o;
    unsigned magic, version, fields;
    int i;
    if (!buf) well_panic("decode(null)");
    d = well_type_desc(type_id);
    if (!d) well_panic("decode: unknown type");
    c.buf = buf;
    c.len = len;
    c.pos = 0;
    magic = rd_u32(&c);
    version = rd_u8(&c);
    if (magic != WELL_SER_MAGIC || version != WELL_SER_VERSION)
        well_panic("decode: bad magic/version");
    if (rd_u32(&c) != type_id) well_panic("decode: type id mismatch");
    fields = rd_u32(&c);
    if (fields != (unsigned)d->field_count) well_panic("decode: field count mismatch");
    o = (WellObj *)well_new(d->size, type_id, oid);
    for (i = 0; i < d->field_count; i++) {
        const WellFieldDesc *f = &d->fields[i];
        void *p = (char *)o + f->offset;
        switch (f->kind) {
        case WELL_FIELD_INT:
            *(int *)p = (int)rd_u64(&c);
            break;
        case WELL_FIELD_LONG:
            *(long long *)p = (long long)rd_u64(&c);
            break;
        case WELL_FIELD_BOOL:
            *(int *)p = (int)(rd_u64(&c) != 0);
            break;
        case WELL_FIELD_DOUBLE: {
            unsigned long long u = rd_u64(&c);
            memcpy(p, &u, 8);
            break;
        }
        case WELL_FIELD_STRING: {
            WellString *s = deser_string(&c);
            well_set_ref((void *)o, (void **)p, s);
            if (s) well_release(s);
            break;
        }
        case WELL_FIELD_REF: {
            unsigned present = rd_u8(&c);
            void *r = NULL;
            if (present) {
                unsigned tid = rd_u32(&c);
                unsigned long long ref_oid = rd_u64(&c);
                r = resolve ? resolve(tid, (long long)ref_oid, user) : NULL;
            }
            well_set_ref((void *)o, (void **)p, r);
            break;
        }
        case WELL_FIELD_ARRAY:
        case WELL_FIELD_MAP:
        case WELL_FIELD_LINKED: {
            void *con = deser_container(&c, f->kind, f->elem_kind, f->elem_type_id,
                                        oid, resolve, user);
            well_set_ref((void *)o, (void **)p, con);
            if (con) well_release(con);
            break;
        }
        default:
            well_panic("decode: unsupported field kind");
        }
    }
    if (d->is_event) well_freeze(o);
    return o;
}
