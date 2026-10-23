#include "wellrt.h"
#include "well_internal.h"

#include <stdlib.h>
#include <string.h>

#define WELL_MAX_MODULES 32
#define WELL_MAX_BUILTIN_EVENTS 64

static WellModule g_modules[WELL_MAX_MODULES];
static int g_module_count = 0;
static int g_modules_inited = 0;
static WellMutex g_module_mutex;
static int g_module_mutex_inited = 0;

typedef struct {
    WellBuiltinEventKind kind;
    unsigned int type_id;
    void *(*factory)(long long oid, void *payload);
    int used;
} BuiltinEventEntry;

static BuiltinEventEntry g_builtin_events[WELL_MAX_BUILTIN_EVENTS];
static WellMutex g_builtin_mutex;
static int g_builtin_mutex_inited = 0;

static void module_lock_init(void) {
    if (!g_module_mutex_inited) {
        well_mutex_init(&g_module_mutex);
        g_module_mutex_inited = 1;
    }
}

static void builtin_lock_init(void) {
    if (!g_builtin_mutex_inited) {
        well_mutex_init(&g_builtin_mutex);
        g_builtin_mutex_inited = 1;
    }
}

void well_module_register(const WellModule *module) {
    int i;
    if (!module) return;
    module_lock_init();
    well_mutex_lock(&g_module_mutex);
    for (i = 0; i < g_module_count; i++) {
        if (g_modules[i].name && module->name &&
            strcmp(g_modules[i].name, module->name) == 0) {
            well_mutex_unlock(&g_module_mutex);
            return;
        }
    }
    if (g_module_count >= WELL_MAX_MODULES) {
        well_mutex_unlock(&g_module_mutex);
        well_panic("too many runtime modules");
    }
    g_modules[g_module_count++] = *module;
    well_mutex_unlock(&g_module_mutex);
}

void well_module_init_all(void) {
    WellModule snapshot[WELL_MAX_MODULES];
    int count, i;
    module_lock_init();
    well_mutex_lock(&g_module_mutex);
    if (g_modules_inited) {
        well_mutex_unlock(&g_module_mutex);
        return;
    }
    count = g_module_count;
    memcpy(snapshot, g_modules, sizeof(WellModule) * (size_t)count);
    well_mutex_unlock(&g_module_mutex);
    /* Call init() outside the module lock: an init callback is allowed to
     * register further modules, and a panic must not leave the lock held. */
    for (i = 0; i < count; i++) {
        if (snapshot[i].init && snapshot[i].init() != 0)
            well_panic("module init failed");
    }
    well_mutex_lock(&g_module_mutex);
    g_modules_inited = 1;
    well_mutex_unlock(&g_module_mutex);
}

void well_module_shutdown_all(void) {
    WellModule snapshot[WELL_MAX_MODULES];
    int count, i;
    module_lock_init();
    well_mutex_lock(&g_module_mutex);
    if (!g_modules_inited) {
        well_mutex_unlock(&g_module_mutex);
        return;
    }
    count = g_module_count;
    memcpy(snapshot, g_modules, sizeof(WellModule) * (size_t)count);
    g_modules_inited = 0;
    well_mutex_unlock(&g_module_mutex);
    /* Callbacks run outside the lock (same reasoning as init). */
    for (i = count - 1; i >= 0; i--) {
        if (snapshot[i].stop) snapshot[i].stop();
        if (snapshot[i].shutdown) snapshot[i].shutdown();
    }
}

void well_builtin_event_register(WellBuiltinEventKind kind,
                                 unsigned int type_id,
                                 void *(*factory)(long long oid, void *payload)) {
    int i, slot = -1;
    builtin_lock_init();
    well_mutex_lock(&g_builtin_mutex);
    for (i = 0; i < WELL_MAX_BUILTIN_EVENTS; i++) {
        if (g_builtin_events[i].used && g_builtin_events[i].kind == kind) {
            slot = i;
            break;
        }
        if (!g_builtin_events[i].used && slot < 0) slot = i;
    }
    if (slot < 0) {
        well_mutex_unlock(&g_builtin_mutex);
        well_panic("too many builtin events");
    }
    g_builtin_events[slot].used = 1;
    g_builtin_events[slot].kind = kind;
    g_builtin_events[slot].type_id = type_id;
    g_builtin_events[slot].factory = factory;
    well_mutex_unlock(&g_builtin_mutex);
}

void *well_builtin_event_new(WellBuiltinEventKind kind, long long oid, void *payload) {
    int i;
    void *(*factory)(long long, void *) = NULL;
    builtin_lock_init();
    well_mutex_lock(&g_builtin_mutex);
    for (i = 0; i < WELL_MAX_BUILTIN_EVENTS; i++) {
        if (g_builtin_events[i].used && g_builtin_events[i].kind == kind) {
            factory = g_builtin_events[i].factory;
            break;
        }
    }
    well_mutex_unlock(&g_builtin_mutex);
    if (!factory) return NULL;
    return factory(oid, payload);
}
