#include "wellrt.h"
#include "well_internal.h"

#include <assert.h>
#include <stdio.h>
#include <stdlib.h>

typedef struct Item {
    WellObj hdr;
    int v;
} Item;

typedef struct EventObj {
    WellObj hdr;
} EventObj;

static const WellFieldDesc item_fields[] = {
    {"v", offsetof(Item, v), WELL_FIELD_INT, 0, 0, 0},
};
static const WellTypeDesc item_desc = {
    "Item", 300, sizeof(Item), 0, 1, item_fields,
};
static const WellTypeDesc event_desc = {
    "EventObj", 301, sizeof(EventObj), 1, 0, NULL,
};

static void item_destroy(void *p) { free(p); }
static void event_destroy(void *p) { free(p); }

int main(void) {
    WellCtx ctx;
    Item *obj, *got, *tmp;
    EventObj *ev;
    long long future;

    well_init();
    well_register_type(300, item_destroy);
    well_register_type(301, event_destroy);
    well_register_type_desc(&item_desc);
    well_register_type_desc(&event_desc);
    well_model_activity_enable();
    well_ctx_init(&ctx, 1);

    obj = (Item *)well_new(sizeof(Item), 300, 1);
    obj->v = 9;
    well_model_set(obj, &ctx);
    well_release(obj);

    /* exec_ref > 0: handler pin protects the row even if rc drops. */
    got = (Item *)well_model_get_ctx(300, 1, &ctx);
    assert(got && got->v == 9);
    future = well_mono_ms() + 10000;
    well_model_cleanup(future, 5000);
    tmp = (Item *)well_model_peek(300, 1);
    assert(tmp != NULL);
    well_release(tmp);
    well_release(got);
    well_ctx_clear(&ctx);

    /* exec_ref == 0 and expired: evict Cache regardless of rc, keep Store. */
    well_ctx_init(&ctx, 1);
    well_model_cleanup(future, 5000);
    assert(well_model_peek(300, 1) == NULL);
    got = (Item *)well_model_get(300, 1);
    assert(got && got->v == 9);
    well_release(got);

    /* Queued event keeps the oid active. */
    ev = (EventObj *)well_new(sizeof(EventObj), 301, 1);
    well_emit_event(ev, 0);
    well_release(ev);
    well_model_cleanup(future, 5000);
    tmp = (Item *)well_model_peek(300, 1);
    assert(tmp != NULL);
    well_release(tmp);

    well_run_loop();
    well_model_cleanup(future, 5000);
    assert(well_model_peek(300, 1) == NULL);

    well_ctx_clear(&ctx);
    well_shutdown();
    printf("inactive oid cleanup ok\n");
    return 0;
}
