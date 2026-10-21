#include "wellrt.h"
#include "well_internal.h"

#include <assert.h>
#include <stdio.h>
#include <stdlib.h>

typedef struct Item {
    WellObj hdr;
    int v;
} Item;

static const WellFieldDesc item_fields[] = {
    {"v", offsetof(Item, v), WELL_FIELD_INT, 0, 0, 0},
};
static const WellTypeDesc item_desc = {
    "Item", 200, sizeof(Item), 0, 1, item_fields,
};

static void item_destroy(void *p) {
    free(p);
}

int main(void) {
    WellCtx ctx;
    Item *obj, *got, *found;
    well_init();
    well_register_type(200, item_destroy);
    well_register_type_desc(&item_desc);
    well_ctx_init(&ctx, 1);

    obj = (Item *)well_new(sizeof(Item), 200, 1);
    obj->v = 77;
    well_model_set(obj, &ctx);
    well_release(obj);

    got = (Item *)well_model_get(200, 1);
    assert(got && got->v == 77);
    well_release(got);

    /* Evict cache only; Store must still hold the row. */
    well_cache_remove(200, 1);
    assert(well_model_peek(200, 1) == NULL);

    got = (Item *)well_model_get(200, 1);
    assert(got && got->v == 77);
    well_release(got);

    found = (Item *)well_model_find_by_oid(200, 1);
    assert(found && found->v == 77);
    well_release(found);

    well_model_remove(200, 1, &ctx);
    assert(well_model_peek(200, 1) == NULL);
    assert(well_model_get(200, 1) == NULL);
    assert(well_model_find_by_oid(200, 1) == NULL);

    well_ctx_clear(&ctx);
    well_shutdown();
    printf("model/store facade ok\n");
    return 0;
}
