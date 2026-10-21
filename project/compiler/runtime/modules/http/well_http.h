#ifndef WELL_HTTP_H
#define WELL_HTTP_H

#include "wellrt.h"
#include "well_internal.h"

#ifdef _WIN32
typedef unsigned long long WellSocket;
#define WELL_INVALID_SOCKET (~0ULL)
#else
typedef int WellSocket;
#define WELL_INVALID_SOCKET (-1)
#endif

typedef struct WellHttpRequest WellHttpRequest;
typedef struct WellHttpConn WellHttpConn;
typedef void (*WellHttpRouteFn)(WellCtx *ctx, WellHttpRequest *req);

/* One live TCP connection; served by a dedicated reader thread that supports
 * keep-alive. refs: 1 for the connection list + 1 per in-flight request. */
struct WellHttpConn {
    WellSocket fd;
    long long id;
    WellMutex lock;
    WellCond cond;
    int closed;
    int refs;
    /* last resolved route (method+path -> route pointer), avoids taking the
     * module lock for repeated requests on a keep-alive connection */
    char cache_method[16];
    char cache_path[256];
    void *cache_route;
    int cache_valid;
    struct WellHttpConn *next;
};

/* Request lifetime is reference counted and guarded by the module lock.
 * refs: 1 for list membership, +1 while the connection thread waits on it.
 * The response socket is shared with the connection; `written` tells the
 * reader thread when the response bytes are on the wire. */
struct WellHttpRequest {
    long long id;
    WellString *method;
    WellString *path;
    WellString *query;
    WellString *body;
    WellHttpConn *conn;
    int responded;
    int written;
    int keep_alive;
    int refs;
    long long deadline_ms;
    struct WellHttpRequest *next;
};

void well_http_route(const char *path, const char *method, WellHttpRouteFn fn);
void well_http_listen(int port);
void well_http_stop(void);
void well_http_respond(long long req_id, int status, const char *body);
void well_http_shutdown(void);

extern WellModule well_http_module;

#endif
