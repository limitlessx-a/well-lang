#include "wellrt.h"
#include "well_http.h"
#include "well_internal.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#ifdef _WIN32
#include <windows.h>
typedef unsigned long long WellSocket;
typedef struct {
    unsigned short family;
    unsigned short port;
    unsigned long addr;
    char zero[8];
} WellSockaddrIn;
#define WELL_INVALID_SOCKET (~0ULL)
static HMODULE g_ws;
static int (*pWSAStartup)(int, void *);
static int (*pWSACleanup)(void);
static WellSocket (*psocket)(int, int, int);
static int (*pbind)(WellSocket, const void *, int);
static int (*plisten)(WellSocket, int);
static WellSocket (*paccept)(WellSocket, void *, int *);
static int (*precv)(WellSocket, char *, int, int);
static int (*psend)(WellSocket, const char *, int, int);
static int (*pclosesocket)(WellSocket);
#else
#include <sys/socket.h>
#include <netinet/in.h>
#include <unistd.h>
#include <signal.h>
typedef int WellSocket;
typedef struct {
    unsigned short family;
    unsigned short port;
    unsigned long addr;
    char zero[8];
} WellSockaddrIn;
#define WELL_INVALID_SOCKET (-1)
#endif

typedef struct HttpRoute {
    char *path;
    char *method;
    WellHttpRouteFn fn;
    struct HttpRoute *next;
} HttpRoute;

static HttpRoute *g_routes = NULL;
static WellMutex g_http_lock;
static WellCond g_http_cond;
static int g_http_inited = 0;
static int g_http_running = 0;
static int g_http_done = 0;
static WellSocket g_http_listen_fd = WELL_INVALID_SOCKET;
static WellThread g_http_thread;
static int g_http_thread_started = 0;
static int g_http_service_held = 0;
static WellHttpRequest *g_current_req = NULL;

static void http_lock_init(void) {
    if (g_http_inited) return;
    well_mutex_init(&g_http_lock);
    well_cond_init(&g_http_cond);
    g_http_inited = 1;
}

#ifdef _WIN32
static void load_winsock(void) {
    if (g_ws) return;
    g_ws = LoadLibraryA("ws2_32.dll");
    if (!g_ws) well_panic("HTTP: cannot load ws2_32.dll");
    pWSAStartup = (int (*)(int, void *))GetProcAddress(g_ws, "WSAStartup");
    pWSACleanup = (int (*)(void))GetProcAddress(g_ws, "WSACleanup");
    psocket = (WellSocket (*)(int, int, int))GetProcAddress(g_ws, "socket");
    pbind = (int (*)(WellSocket, const void *, int))GetProcAddress(g_ws, "bind");
    plisten = (int (*)(WellSocket, int))GetProcAddress(g_ws, "listen");
    paccept = (WellSocket (*)(WellSocket, void *, int *))GetProcAddress(g_ws, "accept");
    precv = (int (*)(WellSocket, char *, int, int))GetProcAddress(g_ws, "recv");
    psend = (int (*)(WellSocket, const char *, int, int))GetProcAddress(g_ws, "send");
    pclosesocket = (int (*)(WellSocket))GetProcAddress(g_ws, "closesocket");
    if (!pWSAStartup || !psocket || !pbind || !plisten || !paccept ||
        !precv || !psend || !pclosesocket)
        well_panic("HTTP: ws2_32 exports missing");
    {
        char wsa[512];
        memset(wsa, 0, sizeof(wsa));
        if (pWSAStartup(0x0202, wsa) != 0) well_panic("HTTP: WSAStartup failed");
    }
}
static WellSocket http_socket(void) { return psocket(2, 1, 0); }
static int http_bind(WellSocket s, WellSockaddrIn *a) { return pbind(s, a, (int)sizeof(*a)); }
static int http_listen(WellSocket s) { return plisten(s, 16); }
static WellSocket http_accept(WellSocket s) { return paccept(s, NULL, NULL); }
static int http_recv(WellSocket s, char *b, int n) { return precv(s, b, n, 0); }
static int http_send(WellSocket s, const char *b, int n) { return psend(s, b, n, 0); }
static void http_close(WellSocket s) { if (s != WELL_INVALID_SOCKET) pclosesocket(s); }
static unsigned short http_htons(unsigned short v) { return (unsigned short)((v << 8) | (v >> 8)); }
#else
static void load_winsock(void) {
    signal(SIGPIPE, SIG_IGN);
}
static WellSocket http_socket(void) { return socket(AF_INET, SOCK_STREAM, 0); }
static int http_bind(WellSocket s, WellSockaddrIn *a) { return bind(s, (struct sockaddr *)a, sizeof(*a)); }
static int http_listen(WellSocket s) { return listen(s, 16); }
static WellSocket http_accept(WellSocket s) { return accept(s, NULL, NULL); }
static int http_recv(WellSocket s, char *b, int n) { return (int)recv(s, b, (size_t)n, 0); }
static int http_send(WellSocket s, const char *b, int n) { return (int)send(s, b, (size_t)n, 0); }
static void http_close(WellSocket s) { if (s >= 0) close(s); }
static unsigned short http_htons(unsigned short v) { return htons(v); }
#endif

static char *http_strdup(const char *s) {
    size_t n = strlen(s);
    char *p = (char *)malloc(n + 1);
    if (!p) well_panic("out of memory (HTTP)");
    memcpy(p, s, n + 1);
    return p;
}

static char *http_strndup(const char *s, size_t n) {
    char *p = (char *)malloc(n + 1);
    if (!p) well_panic("out of memory (HTTP)");
    memcpy(p, s, n);
    p[n] = '\0';
    return p;
}

static int http_send_all(WellSocket s, const char *p, size_t n) {
    while (n > 0) {
        int k = http_send(s, p, (int)n);
        if (k <= 0) return -1;
        p += k;
        n -= (size_t)k;
    }
    return 0;
}

static const char *http_reason(int status) {
    switch (status) {
    case 200: return "OK";
    case 201: return "Created";
    case 204: return "No Content";
    case 400: return "Bad Request";
    case 404: return "Not Found";
    case 405: return "Method Not Allowed";
    case 413: return "Payload Too Large";
    case 500: return "Internal Server Error";
    default: return "OK";
    }
}

static void http_write_response(WellHttpRequest *req, int status, const char *body) {
    char head[256];
    size_t blen = body ? strlen(body) : 0;
    int n = snprintf(head, sizeof(head),
                     "HTTP/1.1 %d %s\r\nContent-Type: text/plain; charset=utf-8\r\n"
                     "Content-Length: %lu\r\nConnection: close\r\n\r\n",
                     status, http_reason(status), (unsigned long)blen);
    if (n < 0) return;
    http_send_all((WellSocket)req->fd, head, strlen(head));
    if (blen) http_send_all((WellSocket)req->fd, body, blen);
}

void well_http_respond(long long req_id, int status, const char *body) {
    WellHttpRequest *req;
    http_lock_init();
    well_mutex_lock(&g_http_lock);
    req = g_current_req;
    if (!req || req->id != req_id || req->responded) {
        well_mutex_unlock(&g_http_lock);
        return;
    }
    req->responded = 1;
    well_mutex_unlock(&g_http_lock);
    http_write_response(req, status, body);
}

void well_http_route(const char *path, const char *method, WellHttpRouteFn fn) {
    HttpRoute *r;
    if (!path || !method || !fn) well_panic("Http.route: null argument");
    r = (HttpRoute *)malloc(sizeof(HttpRoute));
    if (!r) well_panic("out of memory (HTTP route)");
    r->path = http_strdup(path);
    r->method = http_strdup(method);
    r->fn = fn;
    http_lock_init();
    well_mutex_lock(&g_http_lock);
    r->next = g_routes;
    g_routes = r;
    well_mutex_unlock(&g_http_lock);
}

static HttpRoute *http_find_route(const char *method, const char *path) {
    HttpRoute *r;
    well_mutex_lock(&g_http_lock);
    for (r = g_routes; r; r = r->next) {
        if (strcmp(r->method, method) == 0 && strcmp(r->path, path) == 0)
            break;
    }
    well_mutex_unlock(&g_http_lock);
    return r;
}

static int http_starts_ci(const char *s, const char *pre) {
    while (*pre) {
        char a = *s++, b = *pre++;
        if (a >= 'A' && a <= 'Z') a = (char)(a - 'A' + 'a');
        if (b >= 'A' && b <= 'Z') b = (char)(b - 'A' + 'a');
        if (a != b) return 0;
    }
    return 1;
}

/* Read one request. Returns 0 on success. */
static int http_read_request(WellSocket fd, char **method, char **path,
                             char **query, char **body) {
    char *buf = (char *)malloc(65537);
    size_t cap = 65536;
    size_t len = 0;
    char *hdr_end = NULL;
    size_t body_len = 0;
    char *line, *next;
    char *target = NULL;
    char *m = NULL;
    int content_length = -1;

    if (!buf) well_panic("out of memory (HTTP request)");
    for (;;) {
        int k;
        if (len == cap) {
            free(buf);
            return -1;
        }
        k = http_recv(fd, buf + len, (int)(cap - len));
        if (k <= 0) {
            free(buf);
            return -1;
        }
        len += (size_t)k;
        buf[len] = '\0';
        hdr_end = strstr(buf, "\r\n\r\n");
        if (hdr_end) break;
        if (len > 16384) {
            free(buf);
            return -1;
        }
    }

    line = buf;
    next = strstr(line, "\r\n");
    if (!next) { free(buf); return -1; }
    *next = '\0';
    {
        char *sp1 = strchr(line, ' ');
        char *sp2 = sp1 ? strchr(sp1 + 1, ' ') : NULL;
        if (!sp1 || !sp2) { free(buf); return -1; }
        *sp1 = '\0';
        *sp2 = '\0';
        m = http_strdup(line);
        target = http_strdup(sp1 + 1);
    }

    line = next + 2;
    while (line < hdr_end) {
        char *e = strstr(line, "\r\n");
        if (!e) break;
        *e = '\0';
        if (http_starts_ci(line, "Content-Length:"))
            content_length = atoi(line + 15);
        line = e + 2;
    }
    if (content_length < 0) content_length = 0;
    if (content_length > 1048576) { free(buf); free(m); free(target); return -1; }

    body_len = (size_t)content_length;
    *body = (char *)malloc(body_len + 1);
    if (!*body) well_panic("out of memory (HTTP body)");
    {
        size_t have = len - (size_t)((hdr_end + 4) - buf);
        if (have > body_len) have = body_len;
        memcpy(*body, hdr_end + 4, have);
        while (have < body_len) {
            int k = http_recv(fd, *body + have, (int)(body_len - have));
            if (k <= 0) { free(buf); free(m); free(target); free(*body); *body = NULL; return -1; }
            have += (size_t)k;
        }
        (*body)[body_len] = '\0';
    }

    {
        char *q = strchr(target, '?');
        if (q) {
            *q = '\0';
            *query = http_strdup(q + 1);
        } else {
            *query = http_strdup("");
        }
    }
    *path = target;
    *method = m;
    free(buf);
    return 0;
}

static void http_handle_connection(WellSocket fd) {
    char *method = NULL, *path = NULL, *query = NULL, *body = NULL;
    HttpRoute *route;
    WellHttpRequest *req;
    WellCtx ctx;

    if (http_read_request(fd, &method, &path, &query, &body) != 0) {
        const char *bad = "HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\nConnection: close\r\n\r\n";
        http_send_all(fd, bad, strlen(bad));
        return;
    }
    route = http_find_route(method, path);
    req = (WellHttpRequest *)calloc(1, sizeof(WellHttpRequest));
    if (!req) well_panic("out of memory (HTTP request)");
    req->id = well_id_util_get_id();
    req->method = well_string_new(method);
    req->path = well_string_new(path);
    req->query = well_string_new(query);
    req->body = well_string_new(body);
    req->fd = (long long)fd;

    http_lock_init();
    well_mutex_lock(&g_http_lock);
    g_current_req = req;
    well_mutex_unlock(&g_http_lock);

    if (route) {
        jmp_buf jb;
        jmp_buf *old;
        volatile int failed = 0;
        well_ctx_init(&ctx, req->id);
        old = well_panic_push(&jb);
        if (setjmp(jb) == 0) {
            route->fn(&ctx, req);
            well_panic_pop(old);
        } else {
            failed = 1;
            well_panic_pop(old);
        }
        well_ctx_clear(&ctx);
        if (failed) well_http_respond(req->id, 500, "handler panic");
    } else {
        well_http_respond(req->id, 404, "not found");
    }
    if (!req->responded) well_http_respond(req->id, 500, "no response");

    well_mutex_lock(&g_http_lock);
    if (g_current_req == req) g_current_req = NULL;
    well_mutex_unlock(&g_http_lock);
    well_release(req->method);
    well_release(req->path);
    well_release(req->query);
    well_release(req->body);
    free(req);
    free(method);
    free(path);
    free(query);
    free(body);
}

static void *http_listener_thread(void *arg) {
    (void)arg;
    for (;;) {
        WellSocket s, fd;
        well_mutex_lock(&g_http_lock);
        if (!g_http_running) {
            well_mutex_unlock(&g_http_lock);
            break;
        }
        fd = g_http_listen_fd;
        well_mutex_unlock(&g_http_lock);
        s = http_accept(fd);
        if (s == WELL_INVALID_SOCKET) break;
        http_handle_connection(s);
        http_close(s);
    }
    well_mutex_lock(&g_http_lock);
    g_http_done = 1;
    if (g_http_service_held) {
        g_http_service_held = 0;
        well_mutex_unlock(&g_http_lock);
        well_service_release();
        well_mutex_lock(&g_http_lock);
    }
    g_http_running = 0;
    well_cond_signal(&g_http_cond);
    well_mutex_unlock(&g_http_lock);
    return NULL;
}

void well_http_listen(int port) {
    WellSockaddrIn addr;
    int r;
    http_lock_init();
    load_winsock();
    if (g_http_running) well_panic("Http.listen: already running");
    memset(&addr, 0, sizeof(addr));
    addr.family = 2; /* AF_INET */
    addr.port = http_htons((unsigned short)port);
    addr.addr = 0;
    g_http_listen_fd = http_socket();
    if (g_http_listen_fd == WELL_INVALID_SOCKET) well_panic("Http.listen: socket failed");
    r = http_bind(g_http_listen_fd, &addr);
    if (r != 0) well_panic("Http.listen: bind failed");
    r = http_listen(g_http_listen_fd);
    if (r != 0) well_panic("Http.listen: listen failed");
    g_http_running = 1;
    g_http_done = 0;
    if (well_thread_start(&g_http_thread, http_listener_thread, NULL) != 0)
        well_panic("Http.listen: cannot start thread");
    g_http_thread_started = 1;
    g_http_service_held = 1;
    well_service_hold();
    /* 非阻塞：listener 与 worker 生命周期由 well_run_loop 管理。 */
}

void well_http_stop(void) {
    WellSocket fd;
    http_lock_init();
    well_mutex_lock(&g_http_lock);
    g_http_running = 0;
    fd = g_http_listen_fd;
    g_http_listen_fd = WELL_INVALID_SOCKET;
    well_cond_signal(&g_http_cond);
    well_mutex_unlock(&g_http_lock);
    http_close(fd);
}

void well_http_shutdown(void) {
    if (g_http_running) well_http_stop();
    if (g_http_thread_started) {
        well_thread_join(g_http_thread);
        g_http_thread_started = 0;
    }
    if (g_http_service_held) {
        g_http_service_held = 0;
        well_service_release();
    }
    g_http_running = 0;
}


WellModule well_http_module = { "http", NULL, NULL, NULL, well_http_shutdown };

