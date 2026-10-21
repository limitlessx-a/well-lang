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
    "Item", 601, sizeof(Item), 0, 1, item_fields,
};

static void item_destroy(void *p) { free(p); }

static void *get_worker(void *arg) {
    (void)arg;
    while (1) {
        WellCtx ctx;
        Item *got;
        int i;
        well_ctx_init(&ctx, 1);
        for (i = 0; i < 2000; i++) {
            got = (Item *)well_model_get_ctx(601, 1, &ctx);
            if (!got) break;
            assert(got->v == 7);
            well_release(got);
            well_ctx_clear(&ctx);
            well_ctx_init(&ctx, 1);
        }
        well_ctx_clear(&ctx);
        return NULL;
    }
}

static void *cleanup_worker(void *arg) {
    (void)arg;
    int i;
    for (i = 0; i < 2000; i++) {
        well_model_cleanup(well_mono_ms() + 10000, 5000);
        well_sleep_ms(1);
    }
    return NULL;
}

int main(void) {
    WellCtx ctx;
    Item *obj;
    WellThread t1, t2;

    well_init();
    well_register_type(601, item_destroy);
    well_register_type_desc(&item_desc);
    well_model_activity_enable();
    well_ctx_init(&ctx, 1);

    obj = (Item *)well_new(sizeof(Item), 601, 1);
    obj->v = 7;
    well_model_set(obj, &ctx);
    well_release(obj);

    assert(well_thread_start(&t1, get_worker, NULL) == 0);
    assert(well_thread_start(&t2, cleanup_worker, NULL) == 0);
    well_thread_join(t1);
    well_thread_join(t2);

    well_model_cleanup(well_mono_ms() + 10000, 5000);
    assert(well_model_peek(601, 1) == NULL);
    well_ctx_clear(&ctx);
    well_shutdown();
    printf("cleanup race ok\n");
    return 0;
}
