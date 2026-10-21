#include "wellrt.h"
#include "well_http.h"
#include "well_internal.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#ifdef _WIN32
#include <windows.h>
typedef struct {
    unsigned short family;
    unsigned short port;
    unsigned long addr;
    char zero[8];
} WellSockaddrIn;
static HMODULE g_ws;
static int (*pWSAStartup)(int, void *);
static int (*pWSACleanup)(void);
static WellSocket (*psocket)(int, int, int);
static int (*pbind)(WellSocket, const void *, int);
static int (*plisten)(WellSocket, int);
static WellSocket (*paccept)(WellSocket, void *, int *);
static int (*precv)(WellSocket, char *, int, int);
static int (*psend)(WellSocket, const char *, int, int);
static int (*pshutdown_fn)(WellSocket, int);
static int (*psetsockopt)(WellSocket, int, int, const char *, int);
static int (*pclosesocket)(WellSocket);
#define WELL_SOL_SOCKET 0xFFFF
#define WELL_SO_RCVTIMEO 0x1006
#else
#include <sys/socket.h>
#include <sys/time.h>
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
static WellSocket g_http_listen_fd = WELL_INVALID_SOCKET;
static WellThread g_http_thread;
static int g_http_thread_started = 0;
static int g_http_service_held = 0;
static WellHttpRequest *g_requests = NULL;
static WellHttpConn *g_conns = NULL;
static WellThread g_http_sweep_thread;
static int g_http_sweep_started = 0;
static int g_http_sweep_stop = 0;

/* A request that has not been answered within this window is answered with 504. */
#define WELL_HTTP_TIMEOUT_MS 5000
#define WELL_HTTP_SWEEP_BATCH 32
/* An idle keep-alive connection is closed after this long without a request. */
#define WELL_HTTP_IDLE_TIMEOUT_MS 15000

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
    pshutdown_fn = (int (*)(WellSocket, int))GetProcAddress(g_ws, "shutdown");
    psetsockopt = (int (*)(WellSocket, int, int, const char *, int))GetProcAddress(g_ws, "setsockopt");
    pclosesocket = (int (*)(WellSocket))GetProcAddress(g_ws, "closesocket");
    if (!pWSAStartup || !psocket || !pbind || !plisten || !paccept ||
        !precv || !psend || !pshutdown_fn || !psetsockopt || !pclosesocket)
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
static void http_shutdown_socket(WellSocket s) { if (s != WELL_INVALID_SOCKET) pshutdown_fn(s, 2); }
static int http_set_recv_timeout(WellSocket s, int ms) {
    DWORD tv = (DWORD)ms;
    return psetsockopt(s, WELL_SOL_SOCKET, WELL_SO_RCVTIMEO, (const char *)&tv, (int)sizeof(tv));
}
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
static void http_shutdown_socket(WellSocket s) { if (s >= 0) shutdown(s, SHUT_RDWR); }
static int http_set_recv_timeout(WellSocket s, int ms) {
    struct timeval tv;
    tv.tv_sec = ms / 1000;
    tv.tv_usec = (long)(ms % 1000) * 1000;
    return setsockopt(s, SOL_SOCKET, SO_RCVTIMEO, (const char *)&tv, sizeof(tv));
}
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
    case 502: return "Bad Gateway";
    case 503: return "Service Unavailable";
    case 504: return "Gateway Timeout";
    default: return "OK";
    }
}

static void http_write_response(WellHttpRequest *req, int status, const char *body) {
    char head[320];
    size_t blen = body ? strlen(body) : 0;
    const char *ch = req->keep_alive ? "keep-alive" : "close";
    int n = snprintf(head, sizeof(head),
                     "HTTP/1.1 %d %s\r\nContent-Type: text/plain; charset=utf-8\r\n"
                     "Content-Length: %lu\r\nConnection: %s\r\n\r\n",
                     status, http_reason(status), (unsigned long)blen, ch);
    WellSocket fd;
    if (n < 0) return;
    fd = req->conn ? req->conn->fd : WELL_INVALID_SOCKET;
    if (fd == WELL_INVALID_SOCKET) return;
    http_send_all(fd, head, strlen(head));
    if (blen) http_send_all(fd, body, blen);
}

/* ---------- connection lifetime (all refcount ops hold g_http_lock) ---------- */

static void http_conn_free(WellHttpConn *c) {
    if (!c) return;
    well_mutex_destroy(&c->lock);
    well_cond_destroy(&c->cond);
    free(c);
}

/* Caller holds g_http_lock. Unlinks on the last ref; returns the orphan to free. */
static WellHttpConn *http_conn_unref_locked(WellHttpConn *c) {
    WellHttpConn **pp;
    if (!c) return NULL;
    c->refs--;
    if (c->refs > 0) return NULL;
    pp = &g_conns;
    while (*pp) {
        if (*pp == c) {
            *pp = c->next;
            break;
        }
        pp = &(*pp)->next;
    }
    return c;
}

/* ---------- request lifetime (all refcount ops hold g_http_lock) ---------- */

/* Request id hash: keeps `Http.respond(reqId)` O(1) instead of a list scan.
 * Deletion uses Knuth's backward-shift algorithm, so the table stays dense. */
static WellHttpRequest **g_req_slots = NULL;
static int g_req_slot_cap = 0;
static int g_req_slot_used = 0;

static unsigned long long http_mix(long long id) {
    unsigned long long x = (unsigned long long)id + 0x9e3779b97f4a7c15ULL;
    x ^= x >> 30;
    x *= 0xbf58476d1ce4e5b9ULL;
    x ^= x >> 27;
    x *= 0x94d049bb133111ebULL;
    x ^= x >> 31;
    return x ? x : 1ULL;
}

static void http_req_map_rehash(int cap) {
    WellHttpRequest **ns = (WellHttpRequest **)calloc((size_t)cap, sizeof(WellHttpRequest *));
    unsigned long long mask;
    int i;
    if (!ns) well_panic("out of memory (http request map)");
    mask = (unsigned long long)(cap - 1);
    for (i = 0; i < g_req_slot_cap; i++) {
        WellHttpRequest *r = g_req_slots[i];
        unsigned long long h;
        if (!r) continue;
        h = http_mix(r->id) & mask;
        while (ns[h]) h = (h + 1) & mask;
        ns[h] = r;
    }
    free(g_req_slots);
    g_req_slots = ns;
    g_req_slot_cap = cap;
}

/* Caller holds g_http_lock. */
static void http_req_map_put(WellHttpRequest *req) {
    unsigned long long mask, h;
    if (g_req_slot_cap == 0 || (g_req_slot_used + 1) * 10 >= g_req_slot_cap * 7)
        http_req_map_rehash(g_req_slot_cap ? g_req_slot_cap * 2 : 64);
    mask = (unsigned long long)(g_req_slot_cap - 1);
    h = http_mix(req->id) & mask;
    while (g_req_slots[h]) h = (h + 1) & mask;
    g_req_slots[h] = req;
    g_req_slot_used++;
}

/* Caller holds g_http_lock. */
static WellHttpRequest *http_req_map_find(long long id) {
    unsigned long long mask, h;
    if (g_req_slot_cap == 0) return NULL;
    mask = (unsigned long long)(g_req_slot_cap - 1);
    h = http_mix(id) & mask;
    while (g_req_slots[h]) {
        if (g_req_slots[h]->id == id) return g_req_slots[h];
        h = (h + 1) & mask;
    }
    return NULL;
}

/* Caller holds g_http_lock. */
static void http_req_map_del(long long id) {
    unsigned long long mask, h;
    if (g_req_slot_cap == 0) return;
    mask = (unsigned long long)(g_req_slot_cap - 1);
    h = http_mix(id) & mask;
    while (g_req_slots[h]) {
        if (g_req_slots[h]->id == id) break;
        h = (h + 1) & mask;
    }
    if (!g_req_slots[h]) return;
    for (;;) {
        unsigned long long j = h;
        unsigned long long k;
        g_req_slots[h] = NULL;
        for (;;) {
            j = (j + 1) & mask;
            if (!g_req_slots[j]) {
                g_req_slot_used--;
                return;
            }
            k = http_mix(g_req_slots[j]->id) & mask;
            if (h <= j) {
                if (k <= h || k > j) break;
            } else {
                if (k <= h && k > j) break;
            }
        }
        g_req_slots[h] = g_req_slots[j];
        h = j;
    }
}

static void http_req_free(WellHttpRequest *req) {
    if (req->method) well_release(req->method);
    if (req->path) well_release(req->path);
    if (req->query) well_release(req->query);
    if (req->body) well_release(req->body);
    free(req);
}

/* Caller holds g_http_lock. Unlinks on the last ref; returns the orphan to free. */
static WellHttpRequest *http_req_unref_locked(WellHttpRequest *req) {
    WellHttpRequest **pp;
    req->refs--;
    if (req->refs > 0) return NULL;
    pp = &g_requests;
    while (*pp) {
        if (*pp == req) {
            *pp = req->next;
            break;
        }
        pp = &(*pp)->next;
    }
    http_req_map_del(req->id);
    return req;
}

/* First response wins: whoever claims `responded` writes. The reader thread
 * waits for `written` before reading the next request on a keep-alive socket. */
void well_http_respond(long long req_id, int status, const char *body) {
    WellHttpRequest *req;
    WellHttpRequest *dead;
    WellHttpConn *conn;
    http_lock_init();
    well_mutex_lock(&g_http_lock);
    req = http_req_map_find(req_id);
    if (!req) {
        well_mutex_unlock(&g_http_lock);
        return;
    }
    conn = req->conn;
    well_mutex_lock(&conn->lock);
    well_mutex_unlock(&g_http_lock);
    if (req->responded) {
        well_mutex_unlock(&conn->lock);
        return;
    }
    req->responded = 1;
    well_mutex_unlock(&conn->lock);

    http_write_response(req, status, body);

    well_mutex_lock(&conn->lock);
    req->written = 1;
    if (!req->keep_alive) conn->closed = 1;
    well_cond_signal(&conn->cond);
    well_mutex_unlock(&conn->lock);

    well_mutex_lock(&g_http_lock);
    dead = http_req_unref_locked(req);
    well_mutex_unlock(&g_http_lock);
    if (dead) http_req_free(dead);
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

static int http_has_ci(const char *s, const char *needle) {
    size_t n = strlen(needle);
    while (*s) {
        size_t i;
        for (i = 0; i < n; i++) {
            char a = s[i], b = needle[i];
            if (a >= 'A' && a <= 'Z') a = (char)(a - 'A' + 'a');
            if (b >= 'A' && b <= 'Z') b = (char)(b - 'A' + 'a');
            if (a != b) break;
        }
        if (i == n) return 1;
        s++;
    }
    return 0;
}

/* Reads one request from a keep-alive connection into `buf` (caller-owned
 * scratch, at least 65537 bytes). method/path/query point into `buf` and stay
 * valid until the next call; body is malloc'd (NULL when empty).
 * Returns 0 on success, -1 on a malformed request, -2 when the peer closed
 * (or the idle read timeout elapsed) before sending a new request. */
static int http_read_request(WellSocket fd, char *buf, size_t cap,
                             char **method, char **path, char **query,
                             char **body, int *keep_alive) {
    size_t len = 0;
    char *hdr_end = NULL;
    size_t body_len = 0;
    char *line, *next;
    char *target = NULL;
    int content_length = -1;
    int http11 = 0;
    int conn_close = 0;
    int conn_keep = 0;

    *body = NULL;
    for (;;) {
        int k;
        if (len == cap) return -1;
        k = http_recv(fd, buf + len, (int)(cap - len));
        if (k <= 0) {
            int eof = (len == 0);
            return eof ? -2 : -1;
        }
        len += (size_t)k;
        buf[len] = '\0';
        hdr_end = strstr(buf, "\r\n\r\n");
        if (hdr_end) break;
        if (len > 16384) return -1;
    }

    line = buf;
    next = strstr(line, "\r\n");
    if (!next) return -1;
    *next = '\0';
    {
        char *sp1 = strchr(line, ' ');
        char *sp2 = sp1 ? strchr(sp1 + 1, ' ') : NULL;
        if (!sp1 || !sp2) return -1;
        *sp1 = '\0';
        *sp2 = '\0';
        target = sp1 + 1;
        http11 = (strcmp(sp2 + 1, "HTTP/1.1") == 0);
    }

    line = next + 2;
    while (line < hdr_end) {
        char *e = strstr(line, "\r\n");
        if (!e) break;
        *e = '\0';
        if (http_starts_ci(line, "Content-Length:")) {
            content_length = atoi(line + 15);
        } else if (http_starts_ci(line, "Connection:")) {
            if (http_has_ci(line + 11, "close")) conn_close = 1;
            if (http_has_ci(line + 11, "keep-alive")) conn_keep = 1;
        }
        line = e + 2;
    }
    if (content_length < 0) content_length = 0;
    if (content_length > 1048576) return -1;

    body_len = (size_t)content_length;
    if (body_len > 0) {
        *body = (char *)malloc(body_len + 1);
        if (!*body) well_panic("out of memory (HTTP body)");
        {
            size_t have = len - (size_t)((hdr_end + 4) - buf);
            if (have > body_len) have = body_len;
            memcpy(*body, hdr_end + 4, have);
            while (have < body_len) {
                int k = http_recv(fd, *body + have, (int)(body_len - have));
                if (k <= 0) { free(*body); *body = NULL; return -1; }
                have += (size_t)k;
            }
            (*body)[body_len] = '\0';
        }
    }

    {
        char *q = strchr(target, '?');
        if (q) {
            *q = '\0';
            *query = q + 1;
        } else {
            *query = target + strlen(target);
        }
    }
    *path = target;
    *method = buf;
    *keep_alive = http11 ? !conn_close : conn_keep;
    return 0;
}

typedef struct HttpTaskCtx {
    WellHttpRequest *req;
    HttpRoute *route;
} HttpTaskCtx;

static void http_task_entry(WellCtx *ctx, void *arg) {
    HttpTaskCtx *t = (HttpTaskCtx *)arg;
    if (t->route && t->route->fn) t->route->fn(ctx, t->req);
}

static void http_task_done(void *arg) {
    HttpTaskCtx *t = (HttpTaskCtx *)arg;
    WellHttpRequest *dead;
    well_mutex_lock(&g_http_lock);
    dead = http_req_unref_locked(t->req);
    well_mutex_unlock(&g_http_lock);
    if (dead) http_req_free(dead);
    free(t);
}

/* One reader thread per connection: parse a request, dispatch it, wait until
 * the response bytes are written, then serve the next request when the client
 * asked for keep-alive. */
static void *http_conn_thread(void *arg) {
    WellHttpConn *conn = (WellHttpConn *)arg;
    WellSocket fd = conn->fd;
    char *scratch = (char *)malloc(65537);
    if (!scratch) well_panic("out of memory (HTTP connection buffer)");
    for (;;) {
        char *method = NULL, *path = NULL, *query = NULL, *body = NULL;
        int keep_alive = 0;
        int rc;
        HttpRoute *route;
        WellHttpRequest *req;
        HttpTaskCtx *task;
        long long deadline;
        int written, closed;
        WellHttpRequest *deadreq;
        WellHttpConn *deadconn;

        rc = http_read_request(fd, scratch, 65536, &method, &path, &query, &body, &keep_alive);
        if (rc == -2) break;
        if (rc != 0) {
            const char *bad = "HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\nConnection: close\r\n\r\n";
            http_send_all(fd, bad, strlen(bad));
            break;
        }
        route = http_find_route(method, path);
        req = (WellHttpRequest *)calloc(1, sizeof(WellHttpRequest));
        if (!req) well_panic("out of memory (HTTP request)");
        req->id = well_id_util_get_id();
        req->method = well_string_new(method ? method : "");
        req->path = well_string_new(path ? path : "");
        req->query = well_string_new(query ? query : "");
        req->body = well_string_new(body ? body : "");
        req->conn = conn;
        req->keep_alive = keep_alive;
        req->responded = 0;
        req->written = 0;
        req->refs = 2; /* list reference + this thread */
        req->deadline_ms = well_host_now_ms() + WELL_HTTP_TIMEOUT_MS;
        free(body);

        http_lock_init();
        well_mutex_lock(&g_http_lock);
        req->next = g_requests;
        g_requests = req;
        http_req_map_put(req);
        conn->refs++; /* the in-flight request keeps the connection alive */
        well_mutex_unlock(&g_http_lock);

        if (route) {
            task = (HttpTaskCtx *)calloc(1, sizeof(HttpTaskCtx));
            if (!task) well_panic("out of memory (HTTP task)");
            task->req = req;
            task->route = route;
            well_mutex_lock(&g_http_lock);
            req->refs++; /* task reference */
            well_mutex_unlock(&g_http_lock);
            well_schedule_task(req->id, http_task_entry, task, http_task_done);
        } else {
            well_http_respond(req->id, 404, "not found");
        }

        deadline = well_host_now_ms() + WELL_HTTP_TIMEOUT_MS + 2000;
        well_mutex_lock(&conn->lock);
        while (!req->written && !conn->closed) {
            if (well_host_now_ms() >= deadline) break;
            well_cond_wait(&conn->cond, &conn->lock);
        }
        written = req->written;
        closed = conn->closed;
        well_mutex_unlock(&conn->lock);

        well_mutex_lock(&g_http_lock);
        deadreq = http_req_unref_locked(req); /* this thread's reference */
        deadconn = http_conn_unref_locked(conn); /* request's reference on the conn */
        well_mutex_unlock(&g_http_lock);
        if (deadreq) http_req_free(deadreq);
        if (deadconn) http_conn_free(deadconn); /* cannot happen while we are here */

        if (!written || closed || !keep_alive) break;
    }

    free(scratch);
    {
        WellSocket to_close;
        WellHttpConn *deadconn;
        well_mutex_lock(&g_http_lock);
        conn->closed = 1;
        to_close = conn->fd;
        conn->fd = WELL_INVALID_SOCKET;
        deadconn = http_conn_unref_locked(conn); /* list reference */
        well_mutex_unlock(&g_http_lock);
        if (to_close != WELL_INVALID_SOCKET) http_close(to_close);
        if (deadconn) http_conn_free(deadconn);
    }
    well_service_release();
    return NULL;
}

/* Answers requests that no handler replied to, so sockets never leak. */
static void *http_sweeper_thread(void *arg) {
    (void)arg;
    for (;;) {
        WellHttpRequest *expired[WELL_HTTP_SWEEP_BATCH];
        WellHttpRequest *p;
        int n = 0, i;
        long long now;
        well_sleep_ms(50);
        now = well_host_now_ms();
        http_lock_init();
        well_mutex_lock(&g_http_lock);
        if (g_http_sweep_stop) {
            well_mutex_unlock(&g_http_lock);
            break;
        }
        for (p = g_requests; p && n < WELL_HTTP_SWEEP_BATCH; p = p->next) {
            if (!p->responded && now >= p->deadline_ms) {
                WellHttpConn *c = p->conn;
                well_mutex_lock(&c->lock);
                if (!p->responded) {
                    p->responded = 1;
                    expired[n++] = p;
                }
                well_mutex_unlock(&c->lock);
            }
        }
        well_mutex_unlock(&g_http_lock);
        for (i = 0; i < n; i++) {
            WellHttpRequest *req = expired[i];
            WellHttpConn *c = req->conn;
            WellHttpRequest *dead;
            http_write_response(req, 504, "gateway timeout");
            well_mutex_lock(&c->lock);
            req->written = 1;
            c->closed = 1;
            well_cond_signal(&c->cond);
            well_mutex_unlock(&c->lock);
            well_mutex_lock(&g_http_lock);
            dead = http_req_unref_locked(req);
            well_mutex_unlock(&g_http_lock);
            if (dead) http_req_free(dead);
        }
    }
    return NULL;
}

static void *http_listener_thread(void *arg) {
    WellSocket fd = WELL_INVALID_SOCKET;
    (void)arg;
    for (;;) {
        WellSocket s;
        WellHttpConn *conn;
        WellThread th;
        well_mutex_lock(&g_http_lock);
        if (!g_http_running) {
            well_mutex_unlock(&g_http_lock);
            break;
        }
        fd = g_http_listen_fd;
        well_mutex_unlock(&g_http_lock);
        s = http_accept(fd);
        if (s == WELL_INVALID_SOCKET) break;

        conn = (WellHttpConn *)calloc(1, sizeof(WellHttpConn));
        if (!conn) well_panic("out of memory (HTTP connection)");
        conn->fd = s;
        conn->refs = 1; /* list reference */
        conn->closed = 0;
        well_mutex_init(&conn->lock);
        well_cond_init(&conn->cond);
        http_set_recv_timeout(s, WELL_HTTP_IDLE_TIMEOUT_MS);
        well_service_hold(); /* keep the run loop alive while the client is connected */
        well_mutex_lock(&g_http_lock);
        conn->next = g_conns;
        g_conns = conn;
        well_mutex_unlock(&g_http_lock);
        if (well_thread_start(&th, http_conn_thread, conn) != 0)
            well_panic("HTTP: cannot start connection thread");
        well_thread_detach(th);
    }
    well_mutex_lock(&g_http_lock);
    g_http_running = 0;
    fd = g_http_listen_fd;
    g_http_listen_fd = WELL_INVALID_SOCKET;
    if (g_http_service_held) {
        g_http_service_held = 0;
        well_mutex_unlock(&g_http_lock);
        well_service_release();
    } else {
        well_mutex_unlock(&g_http_lock);
    }
    /* stop() closes the listening socket to unblock accept(); close it here
     * only when accept() failed on its own. */
    if (fd != WELL_INVALID_SOCKET) http_close(fd);
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
    g_http_sweep_stop = 0;
    if (well_thread_start(&g_http_thread, http_listener_thread, NULL) != 0)
        well_panic("Http.listen: cannot start thread");
    g_http_thread_started = 1;
    if (well_thread_start(&g_http_sweep_thread, http_sweeper_thread, NULL) != 0)
        well_panic("Http.listen: cannot start sweeper thread");
    g_http_sweep_started = 1;
    g_http_service_held = 1;
    well_service_hold();
    /* 非阻塞：listener 与 worker 生命周期由 well_run_loop 管理。 */
}

void well_http_stop(void) {
    WellSocket fd;
    WellHttpConn *c;
    http_lock_init();
    well_mutex_lock(&g_http_lock);
    g_http_running = 0;
    fd = g_http_listen_fd;
    g_http_listen_fd = WELL_INVALID_SOCKET;
    for (c = g_conns; c; c = c->next) {
        well_mutex_lock(&c->lock);
        c->closed = 1;
        well_cond_signal(&c->cond);
        well_mutex_unlock(&c->lock);
        /* shutdown() (not close): the reader thread owns the socket and will
         * close it; closing from here can abort the connection with RST. */
        http_shutdown_socket(c->fd);
    }
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
    if (g_http_sweep_started) {
        well_mutex_lock(&g_http_lock);
        g_http_sweep_stop = 1;
        well_mutex_unlock(&g_http_lock);
        well_thread_join(g_http_sweep_thread);
        g_http_sweep_started = 0;
    }
    /* Every connection holds a service count, so the run loop has already
     * waited for all readers; this is a defensive cleanup. */
    for (;;) {
        WellHttpConn *c;
        well_mutex_lock(&g_http_lock);
        c = g_conns;
        if (c) g_conns = c->next;
        well_mutex_unlock(&g_http_lock);
        if (!c) break;
        http_close(c->fd);
        c->fd = WELL_INVALID_SOCKET;
        http_conn_free(c);
    }
    for (;;) {
        WellHttpRequest *req;
        well_mutex_lock(&g_http_lock);
        req = g_requests;
        if (req) g_requests = req->next;
        well_mutex_unlock(&g_http_lock);
        if (!req) break;
        http_req_free(req);
    }
    if (g_http_service_held) {
        g_http_service_held = 0;
        well_service_release();
    }
    g_http_running = 0;
}


WellModule well_http_module = { "http", NULL, NULL, NULL, well_http_shutdown };
