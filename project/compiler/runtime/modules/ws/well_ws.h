#ifndef WELL_WS_H
#define WELL_WS_H

#include "wellrt.h"

typedef struct WellWsClient {
    long long id;
    WellString *path;
    WellString *query;
} WellWsClient;

typedef void (*WellWsOpenFn)(WellCtx *ctx, WellWsClient *client);
typedef void (*WellWsMessageFn)(WellCtx *ctx, WellWsClient *client, WellString *text);
typedef void (*WellWsCloseFn)(WellCtx *ctx, WellWsClient *client, int code, WellString *reason);

void well_ws_route(const char *path, WellWsOpenFn open_fn,
                   WellWsMessageFn msg_fn, WellWsCloseFn close_fn);
void well_ws_listen(int port);
void well_ws_stop(void);
void well_ws_send(long long client_id, const char *text);
void well_ws_close(long long client_id, int code, const char *reason);
void well_ws_shutdown(void);

extern WellModule well_ws_module;

#endif
