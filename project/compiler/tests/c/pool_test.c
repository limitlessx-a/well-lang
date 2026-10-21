#include "wellrt.h"

#include <assert.h>
#include <stdio.h>

typedef struct PObj {
    WellObj hdr;
    int v;
} PObj;

static void pool_destroy(void *p) {
    well_obj_free(p);
}

int main(void) {
    PObj *a, *b;
    well_init();
    well_register_type(500, pool_destroy);
    a = (PObj *)well_new(sizeof(PObj), 500, 1);
    a->v = 7;
    well_release(a);
    b = (PObj *)well_new(sizeof(PObj), 500, 2);
    assert(b == a);       /* same size block should be reused */
    assert(b->v == 0);    /* zeroed before reuse */
    well_release(b);
    well_shutdown();
    printf("pool reuse ok\n");
    return 0;
}
