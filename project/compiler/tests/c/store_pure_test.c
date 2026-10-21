#include "wellrt.h"
#include "well_internal.h"

#include <assert.h>
#include <stdio.h>
#include <stdlib.h>

typedef struct Child {
    WellObj hdr;
    int v;
} Child;

typedef struct Parent {
    WellObj hdr;
    Child *child;
} Parent;

static const WellFieldDesc child_fields[] = {
    {"v", offsetof(Child, v), WELL_FIELD_INT, 0, 0, 0},
};
static const WellTypeDesc child_desc = {
    "Child", 701, sizeof(Child), 0, 1, child_fields,
};
static const WellFieldDesc parent_fields[] = {
    {"child", offsetof(Parent, child), WELL_FIELD_REF, 701, 0, 0},
};
static const WellTypeDesc parent_desc = {
    "Parent", 700, sizeof(Parent), 0, 1, parent_fields,
};

static void child_destroy(void *p) { free(p); }
static void parent_destroy(void *p) {
    Parent *o = (Parent *)p;
    well_release(o->child);
    free(o);
}

int main(void) {
    WellCtx ctx;
    Child *child;
    Parent *parent, *loaded;

    well_init();
    well_register_type(700, parent_destroy);
    well_register_type(701, child_destroy);
    well_register_type_desc(&parent_desc);
    well_register_type_desc(&child_desc);
    well_ctx_init(&ctx, 1);

    child = (Child *)well_new(sizeof(Child), 701, 1);
    child->v = 1;
    well_model_set(child, &ctx);
    well_release(child);

    parent = (Parent *)well_new(sizeof(Parent), 700, 1);
    parent->child = child;
    well_retain(child);
    well_model_set(parent, &ctx);
    well_release(parent);

    /* Cache holds a modified copy; Store still has the original bytes. */
    child = (Child *)well_model_get(701, 1);
    assert(child != NULL);
    child->v = 99;
    well_release(child);

    /* Drop only the parent row; the child cache row remains v=99. */
    well_cache_remove(700, 1);
    loaded = (Parent *)well_model_get(700, 1);
    assert(loaded != NULL);
    assert(loaded->child != NULL);
    assert(loaded->child->v == 1);  /* must come from Store, not Cache */
    well_release(loaded);

    well_ctx_clear(&ctx);
    well_shutdown();
    printf("pure memory store ok\n");
    return 0;
}
