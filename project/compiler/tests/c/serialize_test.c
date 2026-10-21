#include "wellrt.h"

#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

typedef struct Item Item;
struct Item {
    WellObj hdr;
    int i;
    long long l;
    double d;
    int b;
    WellString *s;
    Item *ref;
    WellArray *arr;
    WellMap *map;
    WellLinked *link;
};

/* Field layout uses the same order as the descriptor below. */
static const WellFieldDesc item_fields[] = {
    {"i", offsetof(Item, i), WELL_FIELD_INT, 0, 0, 0},
    {"l", offsetof(Item, l), WELL_FIELD_LONG, 0, 0, 0},
    {"d", offsetof(Item, d), WELL_FIELD_DOUBLE, 0, 0, 0},
    {"b", offsetof(Item, b), WELL_FIELD_BOOL, 0, 0, 0},
    {"s", offsetof(Item, s), WELL_FIELD_STRING, WELL_T_STRING, 0, 0},
    {"ref", offsetof(Item, ref), WELL_FIELD_REF, 100, 0, 0},
    {"arr", offsetof(Item, arr), WELL_FIELD_ARRAY, WELL_T_ARRAY, WELL_FIELD_INT, 0},
    {"map", offsetof(Item, map), WELL_FIELD_MAP, WELL_T_MAP, WELL_FIELD_LONG, 0},
    {"link", offsetof(Item, link), WELL_FIELD_LINKED, WELL_T_LINKED, WELL_FIELD_INT, 0},
};

static const WellTypeDesc item_desc = {
    "Item", 100, sizeof(Item), 0,
    (int)(sizeof(item_fields) / sizeof(item_fields[0])), item_fields,
};

static void *resolve_ref(unsigned int type_id, long long oid, void *user) {
    Item *ref = (Item *)user;
    return (type_id == 100 && oid == ref->hdr.oid) ? ref : NULL;
}

static void item_destroy(void *p) {
    Item *o = (Item *)p;
    well_release(o->s);
    well_release(o->ref);
    well_release(o->arr);
    well_release(o->map);
    well_release(o->link);
    free(o);
}

int main(void) {
    WellCtx ctx;
    Item *a, *ref, *b;
    WellString *s, *k;
    WellArray *arr;
    WellMap *map;
    WellLinked *link;
    unsigned char *buf;
    size_t len = 0;
    long long i;

    well_init();
    well_register_type(100, item_destroy);
    well_register_type_desc(&item_desc);
    well_ctx_init(&ctx, 1);

    ref = (Item *)well_new(sizeof(Item), 100, 2);
    ref->i = 7;

    a = (Item *)well_new(sizeof(Item), 100, 1);
    a->i = 42;
    a->l = 1234567890123LL;
    a->d = 3.5;
    a->b = 1;
    s = well_string_new("hello");
    well_set_ref((void *)a, (void **)&a->s, s);
    well_release(s);
    well_set_ref((void *)a, (void **)&a->ref, ref);
    arr = well_array_new(&ctx, 0);
    for (i = 0; i < 3; i++) well_array_push(arr, well_box_i(i + 1), &ctx);
    well_set_ref((void *)a, (void **)&a->arr, arr);
    well_release(arr);
    map = well_map_new(&ctx, 0);
    k = well_string_new("a");
    well_map_set(map, k, well_box_i(11), &ctx);
    well_release(k);
    k = well_string_new("b");
    well_map_set(map, k, well_box_i(22), &ctx);
    well_release(k);
    well_set_ref((void *)a, (void **)&a->map, map);
    well_release(map);
    link = well_linked_new(&ctx, 0);
    well_linked_push_tail(link, well_box_i(5), &ctx);
    well_linked_push_tail(link, well_box_i(6), &ctx);
    well_set_ref((void *)a, (void **)&a->link, link);
    well_release(link);

    buf = well_encode(a, &len);
    assert(buf && len > 0);
    b = (Item *)well_decode(100, 1, buf, len, resolve_ref, ref);
    free(buf);

    assert(b->hdr.type_id == 100 && b->hdr.oid == 1);
    assert(b->i == 42 && b->l == 1234567890123LL && b->d == 3.5 && b->b == 1);
    assert(b->s && strcmp(well_string_cstr(b->s), "hello") == 0);
    assert(b->ref == ref);
    assert(well_array_len(b->arr) == 3);
    assert(well_array_get(b->arr, 0).i == 1 && well_array_get(b->arr, 2).i == 3);
    assert(well_map_len(b->map) == 2);
    assert(well_map_get(b->map, well_map_key_at(b->map, 0)).i == 11);
    assert(well_linked_len(b->link) == 2);
    assert(well_linked_get(b->link, 1).i == 6);

    well_release(b);
    well_release(a);
    well_release(ref);
    well_ctx_clear(&ctx);
    well_shutdown();
    printf("serialize round-trip ok\n");
    return 0;
}
