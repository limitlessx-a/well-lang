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
    "Item", 600, sizeof(Item), 0, 1, item_fields,
};

static void item_destroy(void *p) { free(p); }

int main(void) {
    WellCtx ctx;
    Item *obj, *got;
    long long future;

    well_init();
    well_register_type(600, item_destroy);
    well_register_type_desc(&item_desc);
    well_model_activity_enable();
    well_ctx_init(&ctx, 1);

    obj = (Item *)well_new(sizeof(Item), 600, 1);
    obj->v = 9;
    well_model_set(obj, &ctx);
    well_release(obj);

    future = well_mono_ms() + 10000;
    well_model_cleanup(future, 5000);
    assert(well_model_peek(600, 1) == NULL);

    got = (Item *)well_model_get(600, 1);
    assert(got != NULL);
    assert(got->v == 9);
    got->v = 99;          /* not written back to Store */
    well_release(got);

    well_model_cleanup(future, 5000);
    got = (Item *)well_model_get(600, 1);
    assert(got != NULL);
    assert(got->v == 9);  /* reloaded old Store bytes */
    well_release(got);

    well_ctx_clear(&ctx);
    well_shutdown();
    printf("store reload ok\n");
    return 0;
}
