#include "wellrt.h"
#include "well_internal.h"

#include <stdlib.h>
#include <time.h>

#ifdef _WIN32
void well_mutex_init(WellMutex *m) { InitializeCriticalSection(m); }
void well_mutex_destroy(WellMutex *m) { DeleteCriticalSection(m); }
void well_mutex_lock(WellMutex *m) { EnterCriticalSection(m); }
void well_mutex_unlock(WellMutex *m) { LeaveCriticalSection(m); }

/* Native condition variables (Vista+) loaded dynamically: TCC's windows.h
 * does not declare CONDITION_VARIABLE. */
static HMODULE g_k32 = NULL;
static void (WINAPI *pInitCV)(void *);
static BOOL (WINAPI *pSleepCV)(void *, CRITICAL_SECTION *, DWORD);
static void (WINAPI *pWakeCV)(void *);
static void (WINAPI *pWakeAllCV)(void *);

static void cond_load(void) {
    if (g_k32) return;
    g_k32 = LoadLibraryA("kernel32.dll");
    if (!g_k32) well_panic("cannot load kernel32.dll");
    pInitCV = (void (WINAPI *)(void *))GetProcAddress(g_k32, "InitializeConditionVariable");
    pSleepCV = (BOOL (WINAPI *)(void *, CRITICAL_SECTION *, DWORD))GetProcAddress(g_k32, "SleepConditionVariableCS");
    pWakeCV = (void (WINAPI *)(void *))GetProcAddress(g_k32, "WakeConditionVariable");
    pWakeAllCV = (void (WINAPI *)(void *))GetProcAddress(g_k32, "WakeAllConditionVariable");
    if (!pInitCV || !pSleepCV || !pWakeCV || !pWakeAllCV)
        well_panic("Windows condition variables unavailable (Vista+ required)");
}

void well_cond_init(WellCond *c) { cond_load(); pInitCV(&c->cv); }
void well_cond_destroy(WellCond *c) { (void)c; }
void well_cond_wait(WellCond *c, WellMutex *m) { pSleepCV(&c->cv, m, INFINITE); }
void well_cond_signal(WellCond *c) { pWakeCV(&c->cv); }
void well_cond_broadcast(WellCond *c) { pWakeAllCV(&c->cv); }

typedef struct {
    void *(*fn)(void *);
    void *arg;
} WellThreadStart;

static DWORD WINAPI well_thread_proc(LPVOID p) {
    WellThreadStart *s = (WellThreadStart *)p;
    void *(*fn)(void *) = s->fn;
    void *arg = s->arg;
    free(s);
    fn(arg);
    return 0;
}

int well_thread_start(WellThread *t, void *(*fn)(void *), void *arg) {
    WellThreadStart *s = (WellThreadStart *)malloc(sizeof(WellThreadStart));
    if (!s) return -1;
    s->fn = fn;
    s->arg = arg;
    *t = CreateThread(NULL, 0, well_thread_proc, s, 0, NULL);
    if (!*t) {
        free(s);
        return -1;
    }
    return 0;
}

void well_thread_join(WellThread t) {
    WaitForSingleObject(t, INFINITE);
    CloseHandle(t);
}

void well_thread_detach(WellThread t) { CloseHandle(t); }

void well_sleep_ms(int ms) { Sleep((DWORD)ms); }
#else
void well_mutex_init(WellMutex *m) { pthread_mutex_init(m, NULL); }
void well_mutex_destroy(WellMutex *m) { pthread_mutex_destroy(m); }
void well_mutex_lock(WellMutex *m) { pthread_mutex_lock(m); }
void well_mutex_unlock(WellMutex *m) { pthread_mutex_unlock(m); }

void well_cond_init(WellCond *c) { pthread_cond_init(c, NULL); }
void well_cond_destroy(WellCond *c) { pthread_cond_destroy(c); }
void well_cond_wait(WellCond *c, WellMutex *m) { pthread_cond_wait(c, m); }
void well_cond_signal(WellCond *c) { pthread_cond_signal(c); }
void well_cond_broadcast(WellCond *c) { pthread_cond_broadcast(c); }

int well_thread_start(WellThread *t, void *(*fn)(void *), void *arg) {
    return pthread_create(t, NULL, fn, arg);
}

void well_thread_join(WellThread t) { pthread_join(t, NULL); }
void well_thread_detach(WellThread t) { pthread_detach(t); }
void well_sleep_ms(int ms) {
    struct timespec ts;
    ts.tv_sec = ms / 1000;
    ts.tv_nsec = (long)(ms % 1000) * 1000000L;
    nanosleep(&ts, NULL);
}
#endif
