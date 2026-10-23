/* R-04 regression: Json.stringify() must not recurse through REF fields without
 * a depth limit. Cyclic structs are legal in the language (see the 40_cycle
 * case), and REF fields recurse through jo_obj() directly, bypassing the depth
 * check in jo_value(); before the fix stringifying a two-node cycle overflowed
 * the stack (0xC00000FD). The fix emits JSON null once depth > 64, so stringify
 * returns a bounded document instead of crashing.
 */
#include "wellrt.h"
#include "well_internal.h"

#include <stdio.h>
#include <string.h>

typedef struct Node {
    WellObj hdr;
    struct Node *next;
    int v;
} Node;

static const WellFieldDesc node_fields[] = {
    {"next", offsetof(Node, next), WELL_FIELD_REF, 700, 0, 0},
    {"v", offsetof(Node, v), WELL_FIELD_INT, 0, 0, 0},
};
static const WellTypeDesc node_desc = {
    "rtfix.Node", 700, sizeof(Node), 0, 2, node_fields,
};

int main(void) {
    Node *a, *b;
    WellString *js;
    int ok;

    well_init();
    well_register_type(700, NULL);
    well_register_type_desc(&node_desc);

    a = (Node *)well_new(sizeof(Node), 700, 0);
    b = (Node *)well_new(sizeof(Node), 700, 0);
    a->v = 1;
    b->v = 2;
    a->next = b;
    b->next = a; /* cycle: legal in the language */

    js = well_json_stringify(a, 0);
    ok = js && well_string_len(js) > 0 && strstr(js->data, "\"v\":1") != NULL &&
         strstr(js->data, "null") != NULL;
    printf("rtfix json cycle: len=%lld ok=%d\n",
           js ? well_string_len(js) : -1LL, ok);
    return ok ? 0 : 1;
}
