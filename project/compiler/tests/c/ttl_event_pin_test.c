#include "wellrt.h"
#include "well_internal.h"

#include <assert.h>
#include <stdio.h>
#include <stdlib.h>

typedef struct Item {
    WellObj hdr;
    int v;
} Item;

typedef struct Msg {
    WellObj hdr;
    Item *item;
} Msg;

static const WellFieldDesc item_fields[] = {
    {"v", offsetof(Item, v), WELL_FIELD_INT, 0, 0, 0},
};
static const WellTypeDesc item_desc = {
    "Item", 400, sizeof(Item), 0, 1, item_fields,
};
static const WellFieldDesc msg_fields[] = {
    {"item", offsetof(Msg, item), WELL_FIELD_REF, 400, 0, 0},
};
static const WellTypeDesc msg_desc = {
    "Msg", 401, sizeof(Msg), 1, 1, msg_fields,
};

static void item_destroy(void *p) { free(p); }
static void msg_destroy(void *p) { free(p); }

int main(void) {
    WellCtx ctx;
    Item *item, *got;
    Msg *ev;
    long long future;

    well_init();
    well_register_type(400, item_destroy);
    well_register_type(401, msg_destroy);
    well_register_type_desc(&item_desc);
    well_register_type_desc(&msg_desc);
    well_model_activity_enable();
    well_ctx_init(&ctx, 42);

    item = (Item *)well_new(sizeof(Item), 400, 42);
    item->v = 7;
    well_model_set(item, &ctx);
    well_release(item);

    ev = (Msg *)well_new(sizeof(Msg), 401, 7);
    ev->item = item;              /* already released local, cache holds */
    well_emit_event(ev, 0);
    well_release(ev);

    future = well_mono_ms() + 10000;
    well_model_cleanup(future, 5000);
    got = (Item *)well_model_peek(400, 42);
    assert(got != NULL);          /* event payload pin prevents eviction */
    well_release(got);

    well_run_loop();
    well_model_cleanup(future, 5000);
    assert(well_model_peek(400, 42) == NULL);

    well_ctx_clear(&ctx);
    well_shutdown();
    printf("ttl event pin ok\n");
    return 0;
}
