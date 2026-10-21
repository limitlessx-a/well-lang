#ifndef WELL_HTTP_H
#define WELL_HTTP_H

#include "wellrt.h"
#include "well_internal.h"

typedef struct WellHttpRequest WellHttpRequest;
typedef void (*WellHttpRouteFn)(WellCtx *ctx, WellHttpRequest *req);

/* Request lifetime is reference counted and guarded by the module lock.
 * refs: 1 for list membership, +1 while the worker task is executing.
 * The response socket is owned by whoever wins the `responded` claim. */
struct WellHttpRequest {
    long long id;
    WellString *method;
    WellString *path;
    WellString *query;
    WellString *body;
    long long fd;
    int responded;
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
