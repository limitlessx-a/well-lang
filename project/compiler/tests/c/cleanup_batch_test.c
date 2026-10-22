#include "wellrt.h"
#include "well_internal.h"

#include <assert.h>
#include <stdio.h>
#include <stdlib.h>

/* Validates the Option-D cleanup: candidate collection is batched, an idle oid
 * group is reclaimed while another oid keeps doing Model.get/set concurrently,
 * and the active oid is never reclaimed. */

typedef struct Item {
    WellObj hdr;
    int v;
} Item;

static const WellFieldDesc item_fields[] = {
    {"v", offsetof(Item, v), WELL_FIELD_INT, 0, 0, 0},
};
static const WellTypeDesc item_desc = {
    "Item", 701, sizeof(Item), 0, 1, item_fields,
};

static void item_destroy(void *p) { free(p); }

#define NOBJ 300
#define KEEP_OID 9999

static volatile int g_stop = 0;

static void *keep_worker(void *arg) {
    (void)arg;
    while (!g_stop) {
        WellCtx ctx;
        Item *o;
        well_ctx_init(&ctx, KEEP_OID);
        o = (Item *)well_model_get_ctx(701, KEEP_OID, &ctx);
        if (!o) {
            o = (Item *)well_new(sizeof(Item), 701, KEEP_OID);
            o->v = 42;
        } else {
            assert(o->v == 42);
        }
        well_model_set(o, &ctx); /* refresh last_active so cleanup must skip it */
        well_release(o);
        well_ctx_clear(&ctx);
        well_sleep_ms(1);
    }
    return NULL;
}

int main(void) {
    WellCtx ctx;
    WellThread t;
    int i, left = 0;

    well_init();
    well_register_type(701, item_destroy);
    well_register_type_desc(&item_desc);
    well_model_activity_enable();

    for (i = 1; i <= NOBJ; i++) {
        Item *o;
        well_ctx_init(&ctx, i);
        o = (Item *)well_new(sizeof(Item), 701, i);
        o->v = i;
        well_model_set(o, &ctx);
        well_release(o);
        well_ctx_clear(&ctx);
    }

    /* Let the 300 objects become idle for longer than the TTL; the keep-oid
     * worker refreshes its oid every millisecond. */
    well_sleep_ms(150);
    assert(well_thread_start(&t, keep_worker, NULL) == 0);

    /* Default batch is 64 oids per call, so several calls are needed. */
    for (i = 0; i < 10; i++) {
        well_model_cleanup(well_mono_ms(), 100);
        well_sleep_ms(5);
    }
    g_stop = 1;
    well_thread_join(t);

    for (i = 1; i <= NOBJ; i++)
        if (well_model_peek(701, i) != NULL) left++;
    assert(left == 0);

    {
        Item *k = (Item *)well_model_peek(701, KEEP_OID);
        assert(k != NULL);
        well_release(k);
    }

    well_shutdown();
    printf("cleanup batch ok\n");
    return 0;
}
