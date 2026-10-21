#include "wellrt.h"
#include "well_internal.h"

#include <assert.h>
#include <stdio.h>

int main(void) {
    WellCtx ctx;
    WellArray *arr;
    WellValue self;
    long long before;

    well_init();
    well_model_activity_enable();
    well_ctx_init(&ctx, 1);

    arr = well_array_new(&ctx, 1);
    self.p = arr;
    well_array_push(arr, self, &ctx);   /* self-cycle */
    well_release(arr);                    /* only refs: cycle */

    before = well_live_count();
    well_model_touch_oid(1);
    well_model_cleanup(well_mono_ms() + 10000, 5000);

    assert(well_live_count() < before);
    well_ctx_clear(&ctx);
    well_shutdown();
    printf("force cleanup ok\n");
    return 0;
}
