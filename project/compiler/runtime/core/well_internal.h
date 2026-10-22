#ifndef WELL_INTERNAL_H
#define WELL_INTERNAL_H

#include "wellrt.h"
#include <setjmp.h>

#if defined(_WIN32)
#include <windows.h>
typedef CRITICAL_SECTION WellMutex;
/* CONDITION_VARIABLE is pointer-sized; wrapped to keep the header dependency-free */
typedef struct WellCondImpl { void *cv; } WellCond;
typedef HANDLE WellThread;
#else
#include <pthread.h>
typedef pthread_mutex_t WellMutex;
typedef pthread_cond_t WellCond;
typedef pthread_t WellThread;
#endif

/* Explicit thread-local storage. Compiler TLS is not usable everywhere:
 * TinyCC 0.9.27 does not parse `__thread`, and its `__declspec(thread)` is a
 * single shared slot rather than per-CreateThread storage. POSIX therefore uses
 * pthread keys; Windows uses Win32 TLS slots (their destructor argument is
 * ignored, so owned state registers a per-thread cleanup instead). */
#if defined(_WIN32)
typedef DWORD WellTlsKey;
#define WELL_TLS_KEY_INVALID TLS_OUT_OF_INDEXES
#else
typedef unsigned int WellTlsKey;
#define WELL_TLS_KEY_INVALID ((WellTlsKey)~0u)
#endif
WellTlsKey well_tls_key_create(void (*destructor)(void *));
void *well_tls_get(WellTlsKey key);
void well_tls_set(WellTlsKey key, void *value);

typedef volatile int WellLock;
void well_lock_acquire(WellLock *l);
void well_lock_release(WellLock *l);

void well_mutex_init(WellMutex *m);
void well_mutex_destroy(WellMutex *m);
void well_mutex_lock(WellMutex *m);
void well_mutex_unlock(WellMutex *m);

void well_cond_init(WellCond *c);
void well_cond_destroy(WellCond *c);
void well_cond_wait(WellCond *c, WellMutex *m);
void well_cond_signal(WellCond *c);
void well_cond_broadcast(WellCond *c);

int well_cpu_count(void);
int well_thread_start(WellThread *t, void *(*fn)(void *), void *arg);
void well_thread_join(WellThread t);
/* Fire-and-forget: the thread cleans itself up when it returns. */
void well_thread_detach(WellThread t);
void well_sleep_ms(int ms);
/* Frees this thread's temporary-object stack. Windows TLS slots have no
 * destructor, so the thread wrapper calls this after the thread body returns;
 * POSIX registers free() as the pthread key destructor instead. */
void well_temp_thread_cleanup(void);

/* Cross-module hooks. Public API stays in wellrt.h. */
void well_containers_register(void);
void well_model_shutdown(void);
void well_event_init(void);
void well_event_shutdown(void);
void well_cache_shutdown(void);
void *well_cache_get(unsigned int type_id, long long oid);
void well_cache_set(void *obj);
void well_cache_remove(unsigned int type_id, long long oid);
int well_cache_evict_oid(long long oid);
int well_cache_evict_oid_all(long long oid);
int well_cache_oid_rows(long long oid);
WellStore *well_memory_store_new(void);
void well_owner_push(int owner);
void well_owner_pop(void);
int well_owner_current(void);
void well_off_owner(int owner);
void well_graph_add_edge(WellObj *from, WellObj *to);
void well_graph_remove_edge(WellObj *from, WellObj *to);
void well_type_debug_dump(void);
long long well_now_ms(void);
long long well_mono_ms(void);
int well_rc_is_one(void *obj);
void well_model_touch_oid(long long oid);
void well_oid_pin(long long oid);
void well_oid_unpin(long long oid);
void well_event_pin_payload(void *event);
void well_event_unpin_payload(void *event);
void well_force_cleanup_oid(long long oid);
void well_pool_sweep(void);
int well_cache_discard_oid(long long oid);
void well_array_force_clear(void *arr);
void well_map_force_clear(void *map);
void well_linked_force_clear(void *linked);
void well_model_activity_enable(void);
void well_model_cleanup(long long now_ms, long long ttl_ms);
void well_model_cleanup_if_due(void);
int well_scheduler_cleanup_enter(void);
void well_scheduler_cleanup_leave(void);
int well_scheduler_begin_cleanup(long long oid);
void well_scheduler_end_cleanup(long long oid);
WellObj *well_oid_take_objects(long long oid);
void well_force_free_objects(WellObj *p);
void well_service_hold(void);
void well_service_release(void);
void well_service_signal(void);

/* Per-thread, per-task panic boundary. */
jmp_buf *well_panic_push(jmp_buf *env);
void well_panic_pop(jmp_buf *old);

#endif
