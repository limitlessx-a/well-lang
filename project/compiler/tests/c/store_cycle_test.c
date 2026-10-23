#include "wellrt.h"
#include "well_internal.h"

#include <assert.h>
#include <stdio.h>
#include <stdlib.h>

/* Regression for cyclic Model data reloaded from the Store: the decoder used
 * to recurse through mem_resolve -> mem_select -> well_decode forever. */

typedef struct B B;

typedef struct A {
    WellObj hdr;
    B *b;
} A;

struct B {
    WellObj hdr;
    A *a;
};

static const WellFieldDesc a_fields[] = {
    {"b", offsetof(A, b), WELL_FIELD_REF, 802, 0, 0},
};
static const WellTypeDesc a_desc = {
    "A", 801, sizeof(A), 0, 1, a_fields,
};
static const WellFieldDesc b_fields[] = {
    {"a", offsetof(B, a), WELL_FIELD_REF, 801, 0, 0},
};
static const WellTypeDesc b_desc = {
    "B", 802, sizeof(B), 0, 1, b_fields,
};

static void a_destroy(void *p) {
    A *o = (A *)p;
    well_release(o->b);
    free(o);
}
static void b_destroy(void *p) {
    B *o = (B *)p;
    well_release(o->a);
    free(o);
}

int main(void) {
    WellCtx ctx;
    A *a, *loaded;
    B *b;

    well_init();
    well_register_type(801, a_destroy);
    well_register_type(802, b_destroy);
    well_register_type_desc(&a_desc);
    well_register_type_desc(&b_desc);
    well_ctx_init(&ctx, 1);

    a = (A *)well_new(sizeof(A), 801, 1);
    b = (B *)well_new(sizeof(B), 802, 1);
    well_set_ref(a, (void **)&a->b, b);
    well_set_ref(b, (void **)&b->a, a);
    well_model_set(a, &ctx);
    well_model_set(b, &ctx);
    well_release(a);
    well_release(b);

    /* Drop cached copies so Model.get must decode the cyclic rows again. */
    well_cache_remove_ctx(&ctx);

    loaded = (A *)well_model_get(801, 1);
    assert(loaded != NULL);
    assert(loaded->b != NULL);
    assert(loaded->b->a == NULL);  /* cycle is broken at the back reference */
    well_release(loaded);          /* a_destroy releases loaded->b */

    well_ctx_clear(&ctx);
    well_shutdown();
    printf("store cycle reload ok\n");
    return 0;
}
