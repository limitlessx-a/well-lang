#include "wellrt.h"
#include "well_internal.h"

#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* Regression for the JSON unknown-field skip path: it used to have no depth
 * limit, so a deeply nested unknown value overflowed the C stack even though
 * the surrounding object was depth-limited. */

typedef struct Item {
    WellObj hdr;
    int v;
} Item;

static const WellFieldDesc item_fields[] = {
    {"v", offsetof(Item, v), WELL_FIELD_INT, 0, 0, 0},
};
static const WellTypeDesc item_desc = {
    "Item", 800, sizeof(Item), 0, 1, item_fields,
};

static void item_destroy(void *p) { free(p); }

int main(void) {
    const int depth = 30000;
    size_t cap = (size_t)depth * 2 + 32;
    char *text = (char *)malloc(cap);
    char *p = text;
    void *obj;
    int i;

    assert(text != NULL);
    well_init();
    well_register_type(800, item_destroy);
    well_register_type_desc(&item_desc);

    /* {"unknown": [[[...]]]} -- "unknown" is not in Item's descriptor, so the
     * parser takes the jskip_value path. */
    memcpy(p, "{\"unknown\":", 11);
    p += 11;
    for (i = 0; i < depth; i++) *p++ = '[';
    for (i = 0; i < depth; i++) *p++ = ']';
    *p++ = '}';
    *p = '\0';

    obj = well_json_try_parse(800, text, 1);
    assert(obj == NULL);  /* rejected by the depth limit, no stack overflow */

    free(text);
    well_shutdown();
    printf("json deep nesting rejected\n");
    return 0;
}
