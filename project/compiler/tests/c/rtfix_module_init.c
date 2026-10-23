#include "wellrt.h"
#include "well_internal.h"

#include <assert.h>
#include <stdio.h>

/* Regression for module init/shutdown holding the module lock while running
 * callbacks: a module that registers another module from its init() used to
 * deadlock (non-recursive mutex, and a panic would strand the lock). */

static int g_sibling_inited = 0;
static int g_sibling_stopped = 0;

static int sibling_init(void) {
    g_sibling_inited = 1;
    return 0;
}

static void sibling_stop(void) {
    g_sibling_stopped = 1;
}

static const WellModule sibling = {
    "rtfix-sibling", sibling_init, NULL, sibling_stop, NULL,
};

static int parent_init(void) {
    /* Nested registration while init_all() used to hold g_module_mutex. */
    well_module_register(&sibling);
    return 0;
}

static const WellModule parent = {
    "rtfix-parent", parent_init, NULL, NULL, NULL,
};

int main(void) {
    well_init();
    well_module_register(&parent);
    /* init_all() snapshots the table, runs callbacks outside the lock, then
     * repeats if a callback registered another module (nested registration). */
    well_module_init_all();   /* used to deadlock on the module mutex */
    assert(g_sibling_inited == 1);
    well_shutdown();          /* runs module shutdown_all() */
    assert(g_sibling_stopped == 1);
    printf("module init nested register ok\n");
    return 0;
}
