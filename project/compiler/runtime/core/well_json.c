#include "wellrt.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <setjmp.h>
#include <math.h>

typedef struct {
    const char *s;
    size_t len;
    size_t pos;
    int depth;
    jmp_buf *err_env;   /* non-NULL: jump here instead of panicking (tryParse) */
    void *root;         /* top-level object, released if parsing fails */
} JsonParser;

typedef struct {
    char *p;
    size_t len;
    size_t cap;
} JsonStr;

static void jerr(JsonParser *p, const char *msg) {
    char buf[256];
    snprintf(buf, sizeof(buf), "JSON parse error at %lu: %s",
             (unsigned long)p->pos, msg);
    if (p->err_env) longjmp(*p->err_env, 1);
    well_panic(buf);
}

static void jskip_ws(JsonParser *p) {
    while (p->pos < p->len) {
        char c = p->s[p->pos];
        if (c == ' ' || c == '\t' || c == '\n' || c == '\r') p->pos++;
        else break;
    }
}

static int jpeek(JsonParser *p) {
    jskip_ws(p);
    return p->pos < p->len ? (unsigned char)p->s[p->pos] : -1;
}

static void jexpect(JsonParser *p, char c) {
    if (jpeek(p) != (unsigned char)c) {
        char msg[32];
        snprintf(msg, sizeof(msg), "expected '%c'", c);
        jerr(p, msg);
    }
    p->pos++;
}

static void jstr_push(JsonStr *b, char c) {
    if (b->len + 1 >= b->cap) {
        size_t ncap = b->cap ? b->cap * 2 : 32;
        char *np = (char *)realloc(b->p, ncap);
        if (!np) well_panic("out of memory (JSON string)");
        b->p = np;
        b->cap = ncap;
    }
    b->p[b->len++] = c;
}

static void jstr_utf8(JsonStr *b, unsigned cp) {
    if (cp <= 0x7F) {
        jstr_push(b, (char)cp);
    } else if (cp <= 0x7FF) {
        jstr_push(b, (char)(0xC0 | (cp >> 6)));
        jstr_push(b, (char)(0x80 | (cp & 0x3F)));
    } else if (cp <= 0xFFFF) {
        jstr_push(b, (char)(0xE0 | (cp >> 12)));
        jstr_push(b, (char)(0x80 | ((cp >> 6) & 0x3F)));
        jstr_push(b, (char)(0x80 | (cp & 0x3F)));
    } else {
        jstr_push(b, (char)(0xF0 | (cp >> 18)));
        jstr_push(b, (char)(0x80 | ((cp >> 12) & 0x3F)));
        jstr_push(b, (char)(0x80 | ((cp >> 6) & 0x3F)));
        jstr_push(b, (char)(0x80 | (cp & 0x3F)));
    }
}

static unsigned jhex4(JsonParser *p) {
    unsigned v = 0;
    int i;
    for (i = 0; i < 4; i++) {
        int c;
        if (p->pos >= p->len) jerr(p, "truncated \\u escape");
        c = (unsigned char)p->s[p->pos++];
        if (c >= '0' && c <= '9') c -= '0';
        else if (c >= 'a' && c <= 'f') c = c - 'a' + 10;
        else if (c >= 'A' && c <= 'F') c = c - 'A' + 10;
        else jerr(p, "bad \\u escape");
        v = (v << 4) | (unsigned)c;
    }
    return v;
}

static char *jparse_string_alloc(JsonParser *p) {
    JsonStr b;
    memset(&b, 0, sizeof(b));
    if (jpeek(p) != '"') jerr(p, "expected string");
    p->pos++;
    while (p->pos < p->len) {
        unsigned char c = (unsigned char)p->s[p->pos++];
        if (c == '"') {
            jstr_push(&b, '\0');
            return b.p;
        }
        if (c == '\\') {
            unsigned char e;
            if (p->pos >= p->len) jerr(p, "truncated escape");
            e = (unsigned char)p->s[p->pos++];
            switch (e) {
            case '"': jstr_push(&b, '"'); break;
            case '\\': jstr_push(&b, '\\'); break;
            case '/': jstr_push(&b, '/'); break;
            case 'b': jstr_push(&b, '\b'); break;
            case 'f': jstr_push(&b, '\f'); break;
            case 'n': jstr_push(&b, '\n'); break;
            case 'r': jstr_push(&b, '\r'); break;
            case 't': jstr_push(&b, '\t'); break;
            case 'u': {
                unsigned cp = jhex4(p);
                if (cp >= 0xD800 && cp <= 0xDBFF) {
                    unsigned lo;
                    if (p->pos + 1 >= p->len || p->s[p->pos] != '\\' ||
                        p->s[p->pos + 1] != 'u')
                        jerr(p, "missing low surrogate");
                    p->pos += 2;
                    lo = jhex4(p);
                    if (lo < 0xDC00 || lo > 0xDFFF)
                        jerr(p, "bad low surrogate");
                    cp = 0x10000 + ((cp - 0xD800) << 10) + (lo - 0xDC00);
                }
                jstr_utf8(&b, cp);
                break;
            }
            default: jerr(p, "bad escape");
            }
        } else {
            if (c < 0x20) jerr(p, "control character in string");
            jstr_push(&b, (char)c);
        }
    }
    jerr(p, "unterminated string");
    return NULL;
}

static int jmatch_lit(JsonParser *p, const char *lit) {
    size_t n = strlen(lit);
    if (p->pos + n > p->len) return 0;
    if (memcmp(p->s + p->pos, lit, n) != 0) return 0;
    p->pos += n;
    return 1;
}

static double jparse_double(JsonParser *p) {
    size_t start;
    char *end = NULL;
    char tmp[128];
    double v;
    jskip_ws(p);
    start = p->pos;
    while (p->pos < p->len) {
        char c = p->s[p->pos];
        if ((c >= '0' && c <= '9') || c == '-' || c == '+' ||
            c == '.' || c == 'e' || c == 'E')
            p->pos++;
        else
            break;
    }
    if (p->pos == start || p->pos - start >= sizeof(tmp))
        jerr(p, "bad number");
    memcpy(tmp, p->s + start, p->pos - start);
    tmp[p->pos - start] = '\0';
    v = strtod(tmp, &end);
    if (!end || *end != '\0') jerr(p, "bad number");
    return v;
}

static long long jparse_long(JsonParser *p) {
    size_t start;
    long long v = 0;
    int neg = 0;
    int digits = 0;
    jskip_ws(p);
    start = p->pos;
    if (p->pos < p->len && p->s[p->pos] == '-') {
        neg = 1;
        p->pos++;
    }
    while (p->pos < p->len && p->s[p->pos] >= '0' && p->s[p->pos] <= '9') {
        v = v * 10 + (p->s[p->pos] - '0');
        p->pos++;
        digits++;
    }
    if (!digits) {
        p->pos = start;
        jerr(p, "bad integer");
    }
    return neg ? -v : v;
}

static void jskip_value(JsonParser *p);

static void jskip_object(JsonParser *p) {
    jexpect(p, '{');
    if (jpeek(p) == '}') { p->pos++; return; }
    for (;;) {
        char *k = jparse_string_alloc(p);
        free(k);
        jexpect(p, ':');
        jskip_value(p);
        if (jpeek(p) == ',') { p->pos++; continue; }
        jexpect(p, '}');
        return;
    }
}

static void jskip_array(JsonParser *p) {
    jexpect(p, '[');
    if (jpeek(p) == ']') { p->pos++; return; }
    for (;;) {
        jskip_value(p);
        if (jpeek(p) == ',') { p->pos++; continue; }
        jexpect(p, ']');
        return;
    }
}

static void jskip_value(JsonParser *p) {
    int c = jpeek(p);
    if (c == '{') jskip_object(p);
    else if (c == '[') jskip_array(p);
    else if (c == '"') { free(jparse_string_alloc(p)); }
    else if (c == 't') { if (!jmatch_lit(p, "true")) jerr(p, "bad true"); }
    else if (c == 'f') { if (!jmatch_lit(p, "false")) jerr(p, "bad false"); }
    else if (c == 'n') { if (!jmatch_lit(p, "null")) jerr(p, "bad null"); }
    else if (c == '-' || (c >= '0' && c <= '9')) { (void)jparse_double(p); }
    else jerr(p, "bad value");
}

typedef struct {
    JsonParser *p;
    long long oid;
} JsonCtx;

static void *jparse_object(JsonParser *p, const WellTypeDesc *d, long long oid);

static void *jparse_ref(JsonParser *p, unsigned int type_id, long long oid) {
    const WellTypeDesc *d;
    if (jpeek(p) == 'n') {
        if (!jmatch_lit(p, "null")) jerr(p, "bad null");
        return NULL;
    }
    d = well_type_desc(type_id);
    if (!d) jerr(p, "ref type has no metadata");
    if (jpeek(p) != '{') jerr(p, "ref expects object");
    return jparse_object(p, d, oid);
}

static WellValue jparse_elem(JsonParser *p, int kind, unsigned int type_id,
                             long long oid, void **owned) {
    WellValue v;
    v.i = 0;
    if (owned) *owned = NULL;
    switch (kind) {
    case WELL_FIELD_INT:
    case WELL_FIELD_LONG:
        v.i = jparse_long(p);
        break;
    case WELL_FIELD_BOOL: {
        int c = jpeek(p);
        if (c == 't') { if (!jmatch_lit(p, "true")) jerr(p, "bad true"); v.i = 1; }
        else if (c == 'f') { if (!jmatch_lit(p, "false")) jerr(p, "bad false"); v.i = 0; }
        else jerr(p, "expected bool");
        break;
    }
    case WELL_FIELD_DOUBLE:
        v.d = jparse_double(p);
        break;
    case WELL_FIELD_STRING: {
        char *s = jparse_string_alloc(p);
        WellString *ws = well_string_new(s);
        free(s);
        v.p = ws;
        if (owned) *owned = ws;
        break;
    }
    case WELL_FIELD_REF: {
        void *r = jparse_ref(p, type_id, oid);
        v.p = r;
        if (owned) *owned = r;
        break;
    }
    default:
        jerr(p, "unsupported element type");
    }
    return v;
}

static void *jparse_container(JsonParser *p, int kind, int elem_kind,
                              unsigned int elem_type_id, long long oid) {
    WellCtx ctx;
    int is_ref = (elem_kind == WELL_FIELD_STRING || elem_kind == WELL_FIELD_REF);
    well_ctx_init(&ctx, oid);
    if (kind == WELL_FIELD_ARRAY) {
        WellArray *a = well_array_new(&ctx, is_ref);
        jexpect(p, '[');
        if (jpeek(p) == ']') { p->pos++; }
        else {
            for (;;) {
                void *owned = NULL;
                WellValue v = jparse_elem(p, elem_kind, elem_type_id, oid, &owned);
                well_array_push(a, v, &ctx);
                if (owned) well_release(owned);
                if (jpeek(p) == ',') { p->pos++; continue; }
                jexpect(p, ']');
                break;
            }
        }
        well_ctx_clear(&ctx);
        return a;
    }
    if (kind == WELL_FIELD_MAP) {
        WellMap *m = well_map_new(&ctx, is_ref);
        jexpect(p, '{');
        if (jpeek(p) == '}') { p->pos++; }
        else {
            for (;;) {
                char *raw = jparse_string_alloc(p);
                WellString *key = well_string_new(raw);
                void *owned = NULL;
                WellValue v;
                free(raw);
                jexpect(p, ':');
                v = jparse_elem(p, elem_kind, elem_type_id, oid, &owned);
                well_map_set(m, key, v, &ctx);
                well_release(key);
                if (owned) well_release(owned);
                if (jpeek(p) == ',') { p->pos++; continue; }
                jexpect(p, '}');
                break;
            }
        }
        well_ctx_clear(&ctx);
        return m;
    }
    if (kind == WELL_FIELD_LINKED) {
        WellLinked *l = well_linked_new(&ctx, is_ref);
        jexpect(p, '[');
        if (jpeek(p) == ']') { p->pos++; }
        else {
            for (;;) {
                void *owned = NULL;
                WellValue v = jparse_elem(p, elem_kind, elem_type_id, oid, &owned);
                well_linked_push_tail(l, v, &ctx);
                if (owned) well_release(owned);
                if (jpeek(p) == ',') { p->pos++; continue; }
                jexpect(p, ']');
                break;
            }
        }
        well_ctx_clear(&ctx);
        return l;
    }
    well_ctx_clear(&ctx);
    jerr(p, "unsupported container type");
    return NULL;
}

static void jparse_field(JsonParser *p, WellObj *obj, const WellFieldDesc *f, long long oid) {
    void *slot = (char *)obj + f->offset;
    switch (f->kind) {
    case WELL_FIELD_INT:
        *(int *)slot = (int)jparse_long(p);
        break;
    case WELL_FIELD_LONG:
        *(long long *)slot = jparse_long(p);
        break;
    case WELL_FIELD_BOOL: {
        int c = jpeek(p);
        if (c == 't') { if (!jmatch_lit(p, "true")) jerr(p, "bad true"); *(int *)slot = 1; }
        else if (c == 'f') { if (!jmatch_lit(p, "false")) jerr(p, "bad false"); *(int *)slot = 0; }
        else jerr(p, "expected bool");
        break;
    }
    case WELL_FIELD_DOUBLE:
        *(double *)slot = jparse_double(p);
        break;
    case WELL_FIELD_STRING: {
        WellString *s;
        char *raw;
        if (jpeek(p) == 'n') {
            if (!jmatch_lit(p, "null")) jerr(p, "bad null");
            well_set_ref(obj, (void **)slot, NULL);
            break;
        }
        raw = jparse_string_alloc(p);
        s = well_string_new(raw);
        free(raw);
        well_set_ref(obj, (void **)slot, s);
        well_release(s);
        break;
    }
    case WELL_FIELD_REF: {
        void *r = jparse_ref(p, f->type_id, oid);
        well_set_ref(obj, (void **)slot, r);
        if (r) well_release(r);
        break;
    }
    case WELL_FIELD_ARRAY:
    case WELL_FIELD_MAP:
    case WELL_FIELD_LINKED: {
        void *c = NULL;
        if (jpeek(p) == 'n') {
            if (!jmatch_lit(p, "null")) jerr(p, "bad null");
            well_set_ref(obj, (void **)slot, NULL);
            break;
        }
        c = jparse_container(p, f->kind, f->elem_kind, f->elem_type_id, oid);
        well_set_ref(obj, (void **)slot, c);
        well_release(c);
        break;
    }
    default:
        jerr(p, "unsupported field type");
    }
}

static void *jparse_object(JsonParser *p, const WellTypeDesc *d, long long oid) {
    WellObj *o;
    if (++p->depth > 128) jerr(p, "nesting too deep");
    o = (WellObj *)well_new(d->size, d->type_id, oid);
    if (!p->root) p->root = o;
    jexpect(p, '{');
    if (jpeek(p) == '}') {
        p->pos++;
    } else {
        for (;;) {
            char *key = jparse_string_alloc(p);
            const WellFieldDesc *f;
            jexpect(p, ':');
            f = well_type_field(d, key);
            free(key);
            if (f) jparse_field(p, o, f, oid);
            else jskip_value(p);
            if (jpeek(p) == ',') { p->pos++; continue; }
            jexpect(p, '}');
            break;
        }
    }
    if (d->is_event) well_freeze(o);
    p->depth--;
    return o;
}


/* ---------- Json.stringify (runtime reflection over WellTypeDesc) ---------- */

typedef struct JOut {
    char *p;
    size_t len;
    size_t cap;
    int indent;
} JOut;

static void jo_need(JOut *o, size_t extra) {
    if (o->len + extra + 1 <= o->cap) return;
    {
        size_t ncap = o->cap ? o->cap * 2 : 256;
        while (ncap < o->len + extra + 1) ncap *= 2;
        o->p = (char *)realloc(o->p, ncap);
        if (!o->p) well_panic("out of memory (Json.stringify)");
        o->cap = ncap;
    }
}

static void jo_putc(JOut *o, char c) {
    jo_need(o, 1);
    o->p[o->len++] = c;
}

static void jo_puts_len(JOut *o, const char *s, size_t n) {
    jo_need(o, n);
    memcpy(o->p + o->len, s, n);
    o->len += n;
}

static void jo_puts(JOut *o, const char *s) { jo_puts_len(o, s, strlen(s)); }

static void jo_long(JOut *o, long long v) {
    char buf[32];
    int n = snprintf(buf, sizeof(buf), "%lld", v);
    jo_puts_len(o, buf, (size_t)n);
}

static void jo_double(JOut *o, double v) {
    char buf[64];
    int n;
    if (!isfinite(v)) {
        jo_puts(o, "null");
        return;
    }
    n = snprintf(buf, sizeof(buf), "%.17g", v);
    jo_puts_len(o, buf, (size_t)n);
}

static void jo_byte_escaped(JOut *o, unsigned char c) {
    char buf[8];
    switch (c) {
    case '"': jo_puts(o, "\\\""); break;
    case '\\': jo_puts(o, "\\\\"); break;
    case '\b': jo_puts(o, "\\b"); break;
    case '\f': jo_puts(o, "\\f"); break;
    case '\n': jo_puts(o, "\\n"); break;
    case '\r': jo_puts(o, "\\r"); break;
    case '\t': jo_puts(o, "\\t"); break;
    default:
        if (c < 0x20) {
            int n = snprintf(buf, sizeof(buf), "\\u%04x", (unsigned)c);
            jo_puts_len(o, buf, (size_t)n);
        } else {
            jo_putc(o, (char)c);
        }
    }
}

static void jo_escaped(JOut *o, const char *s, long long n) {
    long long i;
    jo_putc(o, '"');
    if (s) {
        for (i = 0; i < n; i++)
            jo_byte_escaped(o, (unsigned char)s[i]);
    }
    jo_putc(o, '"');
}

static void jo_nl(JOut *o, int depth) {
    int i, n;
    if (o->indent <= 0) return;
    jo_putc(o, '\n');
    n = depth * o->indent;
    for (i = 0; i < n; i++) jo_putc(o, ' ');
}

static void jo_sep(JOut *o, int depth, int first) {
    if (!first) jo_putc(o, ',');
    jo_nl(o, depth);
}

static void jo_key(JOut *o, const char *k) {
    jo_escaped(o, k, (long long)strlen(k));
    jo_putc(o, ':');
    if (o->indent > 0) jo_putc(o, ' ');
}

static void jo_value(JOut *o, WellValue v, int kind, unsigned int type_id, int depth);
static void jo_obj(JOut *o, void *obj, const WellTypeDesc *d, int depth);

static void jo_array_elems(JOut *o, WellArray *a, int elem_kind, unsigned int elem_type_id, int depth) {
    long long n, j;
    if (!a) { jo_puts(o, "null"); return; }
    jo_putc(o, '[');
    n = well_array_len(a);
    for (j = 0; j < n; j++) {
        jo_sep(o, depth + 1, j == 0);
        jo_value(o, well_array_get(a, j), elem_kind, elem_type_id, depth + 1);
    }
    if (n > 0) jo_nl(o, depth);
    jo_putc(o, ']');
}

static void jo_map_elems(JOut *o, WellMap *m, int elem_kind, unsigned int elem_type_id, int depth) {
    long long n, j;
    if (!m) { jo_puts(o, "null"); return; }
    jo_putc(o, '{');
    n = well_map_len(m);
    for (j = 0; j < n; j++) {
        WellString *k = well_map_key_at(m, j);
        jo_sep(o, depth + 1, j == 0);
        jo_escaped(o, k ? k->data : "", k ? k->len : 0);
        jo_putc(o, ':');
        if (o->indent > 0) jo_putc(o, ' ');
        jo_value(o, well_map_value_at(m, j), elem_kind, elem_type_id, depth + 1);
    }
    if (n > 0) jo_nl(o, depth);
    jo_putc(o, '}');
}

static void jo_linked_elems(JOut *o, WellLinked *l, int elem_kind, unsigned int elem_type_id, int depth) {
    WellLinkedNode *n;
    int first = 1;
    if (!l) { jo_puts(o, "null"); return; }
    jo_putc(o, '[');
    for (n = well_linked_first(l); n; n = well_linked_next(n)) {
        jo_sep(o, depth + 1, first);
        first = 0;
        jo_value(o, well_linked_value(n), elem_kind, elem_type_id, depth + 1);
    }
    if (!first) jo_nl(o, depth);
    jo_putc(o, ']');
}

static void jo_obj(JOut *o, void *obj, const WellTypeDesc *d, int depth) {
    int i;
    if (!obj || !d) {
        jo_puts(o, "null");
        return;
    }
    jo_putc(o, '{');
    for (i = 0; i < d->field_count; i++) {
        const WellFieldDesc *f = &d->fields[i];
        char *base = (char *)obj + f->offset;
        jo_sep(o, depth + 1, i == 0);
        jo_key(o, f->name);
        switch (f->kind) {
        case WELL_FIELD_INT:
            jo_long(o, (long long)(*(int *)base));
            break;
        case WELL_FIELD_LONG:
            jo_long(o, *(long long *)base);
            break;
        case WELL_FIELD_DOUBLE:
            jo_double(o, *(double *)base);
            break;
        case WELL_FIELD_BOOL:
            jo_puts(o, (*(int *)base) ? "true" : "false");
            break;
        case WELL_FIELD_STRING: {
            WellString *s = *(WellString **)base;
            if (!s) jo_puts(o, "null");
            else jo_escaped(o, s->data, s->len);
            break;
        }
        case WELL_FIELD_REF: {
            void *r = *(void **)base;
            jo_obj(o, r, r ? well_type_desc(f->type_id) : NULL, depth + 1);
            break;
        }
        case WELL_FIELD_ARRAY:
            jo_array_elems(o, *(WellArray **)base, f->elem_kind, f->elem_type_id, depth + 1);
            break;
        case WELL_FIELD_MAP:
            jo_map_elems(o, *(WellMap **)base, f->elem_kind, f->elem_type_id, depth + 1);
            break;
        case WELL_FIELD_LINKED:
            jo_linked_elems(o, *(WellLinked **)base, f->elem_kind, f->elem_type_id, depth + 1);
            break;
        default:
            jo_puts(o, "null");
        }
    }
    if (d->field_count > 0) jo_nl(o, depth);
    jo_putc(o, '}');
}

static void jo_value(JOut *o, WellValue v, int kind, unsigned int type_id, int depth) {
    if (depth > 64) { jo_puts(o, "null"); return; }
    switch (kind) {
    case WELL_FIELD_INT:
        jo_long(o, v.i);
        break;
    case WELL_FIELD_LONG:
        jo_long(o, v.i);
        break;
    case WELL_FIELD_DOUBLE:
        jo_double(o, v.d);
        break;
    case WELL_FIELD_BOOL:
        jo_puts(o, v.i ? "true" : "false");
        break;
    case WELL_FIELD_STRING: {
        WellString *s = (WellString *)v.p;
        if (!s) jo_puts(o, "null");
        else jo_escaped(o, s->data, s->len);
        break;
    }
    case WELL_FIELD_REF:
        jo_obj(o, v.p, v.p ? well_type_desc(type_id) : NULL, depth + 1);
        break;
    default:
        jo_puts(o, "null");
    }
}

static void jo_begin(JOut *o, int indent) {
    memset(o, 0, sizeof(*o));
    if (indent < 0) indent = 0;
    if (indent > 8) indent = 8;
    o->indent = indent;
}

static WellString *jo_finish(JOut *o) {
    WellString *r;
    jo_need(o, 1);
    o->p[o->len] = 0;
    r = well_string_new(o->p);
    free(o->p);
    return r;
}

WellString *well_json_stringify(void *obj, int indent) {
    JOut o;
    const WellTypeDesc *d;
    jo_begin(&o, indent);
    if (!obj) {
        jo_puts(&o, "null");
        return jo_finish(&o);
    }
    d = well_type_desc(((WellObj *)obj)->type_id);
    if (!d) well_panic("Json.stringify: unsupported value");
    jo_obj(&o, obj, d, 0);
    return jo_finish(&o);
}

WellString *well_json_stringify_array(WellArray *a, int elem_kind, unsigned int elem_type_id, int indent) {
    JOut o;
    jo_begin(&o, indent);
    jo_array_elems(&o, a, elem_kind, elem_type_id, 0);
    return jo_finish(&o);
}

WellString *well_json_stringify_map(WellMap *m, int elem_kind, unsigned int elem_type_id, int indent) {
    JOut o;
    jo_begin(&o, indent);
    jo_map_elems(&o, m, elem_kind, elem_type_id, 0);
    return jo_finish(&o);
}

WellString *well_json_stringify_linked(WellLinked *l, int elem_kind, unsigned int elem_type_id, int indent) {
    JOut o;
    jo_begin(&o, indent);
    jo_linked_elems(&o, l, elem_kind, elem_type_id, 0);
    return jo_finish(&o);
}

WellString *well_json_stringify_long(long long v, int indent) {
    JOut o;
    jo_begin(&o, indent);
    jo_long(&o, v);
    return jo_finish(&o);
}

WellString *well_json_stringify_double(double v, int indent) {
    JOut o;
    jo_begin(&o, indent);
    jo_double(&o, v);
    return jo_finish(&o);
}

WellString *well_json_stringify_bool(int v, int indent) {
    JOut o;
    jo_begin(&o, indent);
    jo_puts(&o, v ? "true" : "false");
    return jo_finish(&o);
}

WellString *well_json_stringify_str(WellString *s, int indent) {
    JOut o;
    jo_begin(&o, indent);
    if (!s) jo_puts(&o, "null");
    else jo_escaped(&o, s->data, s->len);
    return jo_finish(&o);
}

WellString *well_json_stringify_str_take(WellString *s, int indent) {
    WellString *r = well_json_stringify_str(s, indent);
    well_release(s);
    return r;
}

void *well_json_parse(unsigned int type_id, const char *text, long long oid) {
    JsonParser p;
    const WellTypeDesc *d;
    void *obj;
    if (!text) well_panic("Json.parse(null)");
    d = well_type_desc(type_id);
    if (!d) well_panic("Json.parse: unknown type");
    p.s = text;
    p.len = strlen(text);
    p.pos = 0;
    p.depth = 0;
    p.err_env = NULL;
    p.root = NULL;
    if (jpeek(&p) == 'n') {
        if (!jmatch_lit(&p, "null")) jerr(&p, "bad null");
        jskip_ws(&p);
        if (p.pos != p.len) jerr(&p, "trailing characters");
        return NULL;
    }
    if (jpeek(&p) != '{') jerr(&p, "top-level value must be an object");
    obj = jparse_object(&p, d, oid);
    jskip_ws(&p);
    if (p.pos != p.len) jerr(&p, "trailing characters");
    return obj;
}

/* Non-panicking variant: returns NULL on syntax/type errors (and for JSON
 * "null"). Any partially built top-level object is released. */
void *well_json_try_parse(unsigned int type_id, const char *text, long long oid) {
    JsonParser *p;
    const WellTypeDesc *d;
    jmp_buf jb;
    void *obj;
    if (!text) return NULL;
    d = well_type_desc(type_id);
    if (!d) return NULL;
    p = (JsonParser *)calloc(1, sizeof(JsonParser));
    if (!p) return NULL;
    p->s = text;
    p->len = strlen(text);
    p->err_env = &jb;
    if (setjmp(jb) != 0) {
        if (p->root) well_release(p->root);
        free(p);
        return NULL;
    }
    if (jpeek(p) == 'n') {
        if (!jmatch_lit(p, "null")) jerr(p, "bad null");
        jskip_ws(p);
        if (p->pos != p->len) jerr(p, "trailing characters");
        free(p);
        return NULL;
    }
    if (jpeek(p) != '{') jerr(p, "top-level value must be an object");
    obj = jparse_object(p, d, oid);
    jskip_ws(p);
    if (p->pos != p->len) jerr(p, "trailing characters");
    free(p);
    return obj;
}
