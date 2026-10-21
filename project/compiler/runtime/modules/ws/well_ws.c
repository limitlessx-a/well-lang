#include "wellrt.h"
#include "well_ws.h"
#include "well_internal.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#ifdef _WIN32
#include <windows.h>
typedef unsigned long long WsSocket;
typedef struct { unsigned short family, port; unsigned long addr; char zero[8]; } WsSockaddr;
#define WS_INVALID_SOCKET (~0ULL)
static HMODULE g_wsm;
static int (*pWSAStartup)(int, void *);
static WsSocket (*psocket)(int, int, int);
static int (*pbind)(WsSocket, const void *, int);
static int (*plisten)(WsSocket, int);
static WsSocket (*paccept)(WsSocket, void *, int *);
static int (*precv)(WsSocket, char *, int, int);
static int (*psend)(WsSocket, const char *, int, int);
static int (*pclosesocket)(WsSocket);
#else
#include <sys/socket.h>
#include <netinet/in.h>
#include <unistd.h>
#include <signal.h>
typedef int WsSocket;
typedef struct { unsigned short family, port; unsigned long addr; char zero[8]; } WsSockaddr;
#define WS_INVALID_SOCKET (-1)
#endif

typedef struct WsRoute {
    char *path;
    WellWsOpenFn open_fn;
    WellWsMessageFn msg_fn;
    WellWsCloseFn close_fn;
    struct WsRoute *next;
} WsRoute;

static WsRoute *g_ws_routes = NULL;
static WellMutex g_ws_lock;
static WellCond g_ws_cond;
static int g_ws_inited = 0;
static int g_ws_running = 0;
static int g_ws_done = 0;
static WsSocket g_ws_listen_fd = WS_INVALID_SOCKET;
static WellThread g_ws_thread;
static int g_ws_thread_started = 0;
static long long g_ws_current_id = 0;
static WsSocket g_ws_current_fd = WS_INVALID_SOCKET;
static int g_ws_current_closed = 0;

static void ws_lock_init(void) {
    if (g_ws_inited) return;
    well_mutex_init(&g_ws_lock);
    well_cond_init(&g_ws_cond);
    g_ws_inited = 1;
}

#ifdef _WIN32
static void ws_load(void) {
    if (g_wsm) return;
    g_wsm = LoadLibraryA("ws2_32.dll");
    if (!g_wsm) well_panic("WebSocket: cannot load ws2_32.dll");
    pWSAStartup = (int (*)(int, void *))GetProcAddress(g_wsm, "WSAStartup");
    psocket = (WsSocket (*)(int, int, int))GetProcAddress(g_wsm, "socket");
    pbind = (int (*)(WsSocket, const void *, int))GetProcAddress(g_wsm, "bind");
    plisten = (int (*)(WsSocket, int))GetProcAddress(g_wsm, "listen");
    paccept = (WsSocket (*)(WsSocket, void *, int *))GetProcAddress(g_wsm, "accept");
    precv = (int (*)(WsSocket, char *, int, int))GetProcAddress(g_wsm, "recv");
    psend = (int (*)(WsSocket, const char *, int, int))GetProcAddress(g_wsm, "send");
    pclosesocket = (int (*)(WsSocket))GetProcAddress(g_wsm, "closesocket");
    if (!pWSAStartup || !psocket || !pbind || !plisten || !paccept ||
        !precv || !psend || !pclosesocket)
        well_panic("WebSocket: ws2_32 exports missing");
    {
        char wsa[512];
        memset(wsa, 0, sizeof(wsa));
        if (pWSAStartup(0x0202, wsa) != 0) well_panic("WebSocket: WSAStartup failed");
    }
}
static WsSocket ws_socket(void) { return psocket(2, 1, 0); }
static int ws_bind(WsSocket s, WsSockaddr *a) { return pbind(s, a, (int)sizeof(*a)); }
static int ws_listen_socket(WsSocket s) { return plisten(s, 16); }
static WsSocket ws_accept(WsSocket s) { return paccept(s, NULL, NULL); }
static int ws_recv(WsSocket s, char *b, int n) { return precv(s, b, n, 0); }
static int ws_send_socket(WsSocket s, const char *b, int n) { return psend(s, b, n, 0); }
static void ws_close_socket(WsSocket s) { if (s != WS_INVALID_SOCKET) pclosesocket(s); }
static unsigned short ws_htons(unsigned short v) { return (unsigned short)((v << 8) | (v >> 8)); }
#else
static void ws_load(void) { signal(SIGPIPE, SIG_IGN); }
static WsSocket ws_socket(void) { return socket(AF_INET, SOCK_STREAM, 0); }
static int ws_bind(WsSocket s, WsSockaddr *a) { return bind(s, (struct sockaddr *)a, sizeof(*a)); }
static int ws_listen_socket(WsSocket s) { return listen(s, 16); }
static WsSocket ws_accept(WsSocket s) { return accept(s, NULL, NULL); }
static int ws_recv(WsSocket s, char *b, int n) { return (int)recv(s, b, (size_t)n, 0); }
static int ws_send_socket(WsSocket s, const char *b, int n) { return (int)send(s, b, (size_t)n, 0); }
static void ws_close_socket(WsSocket s) { if (s >= 0) close(s); }
static unsigned short ws_htons(unsigned short v) { return htons(v); }
#endif

static int ws_send_all(WsSocket s, const char *p, size_t n) {
    while (n) {
        int k = ws_send_socket(s, p, (int)n);
        if (k <= 0) return -1;
        p += k;
        n -= (size_t)k;
    }
    return 0;
}

static char *ws_dup(const char *s) {
    size_t n = strlen(s);
    char *p = (char *)malloc(n + 1);
    if (!p) well_panic("out of memory (WebSocket)");
    memcpy(p, s, n + 1);
    return p;
}

/* ---------- SHA1 + Base64 (handshake) ---------- */
typedef struct {
    unsigned int h[5];
    unsigned long long len;
    unsigned char buf[64];
    size_t n;
} Sha1;

static unsigned int rol(unsigned int x, int n) { return (x << n) | (x >> (32 - n)); }

static void sha1_block(Sha1 *s, const unsigned char *p) {
    unsigned int w[80], a, b, c, d, e;
    int i;
    for (i = 0; i < 16; i++)
        w[i] = ((unsigned)p[i*4] << 24) | ((unsigned)p[i*4+1] << 16) |
               ((unsigned)p[i*4+2] << 8) | p[i*4+3];
    for (i = 16; i < 80; i++) w[i] = rol(w[i-3]^w[i-8]^w[i-14]^w[i-16], 1);
    a = s->h[0]; b = s->h[1]; c = s->h[2]; d = s->h[3]; e = s->h[4];
    for (i = 0; i < 80; i++) {
        unsigned f, k, t;
        if (i < 20) { f = (b & c) | ((~b) & d); k = 0x5A827999u; }
        else if (i < 40) { f = b ^ c ^ d; k = 0x6ED9EBA1u; }
        else if (i < 60) { f = (b & c) | (b & d) | (c & d); k = 0x8F1BBCDCu; }
        else { f = b ^ c ^ d; k = 0xCA62C1D6u; }
        t = rol(a,5) + f + e + k + w[i];
        e = d; d = c; c = rol(b,30); b = a; a = t;
    }
    s->h[0] += a; s->h[1] += b; s->h[2] += c; s->h[3] += d; s->h[4] += e;
}

static void sha1_init(Sha1 *s) {
    s->h[0] = 0x67452301u; s->h[1] = 0xEFCDAB89u; s->h[2] = 0x98BADCFEu;
    s->h[3] = 0x10325476u; s->h[4] = 0xC3D2E1F0u;
    s->len = 0; s->n = 0;
}

static void sha1_update(Sha1 *s, const void *data, size_t n) {
    const unsigned char *p = (const unsigned char *)data;
    s->len += (unsigned long long)n * 8;
    while (n) {
        size_t take = 64 - s->n;
        if (take > n) take = n;
        memcpy(s->buf + s->n, p, take);
        s->n += take; p += take; n -= take;
        if (s->n == 64) { sha1_block(s, s->buf); s->n = 0; }
    }
}

static void sha1_final(Sha1 *s, unsigned char out[20]) {
    unsigned char pad[72];
    size_t padn;
    unsigned long long bits = s->len;
    int i;
    pad[0] = 0x80;
    padn = (s->n < 56) ? (56 - s->n) : (120 - s->n);
    memset(pad + 1, 0, padn - 1);
    sha1_update(s, pad, padn);
    for (i = 0; i < 8; i++) pad[i] = (unsigned char)(bits >> (56 - i*8));
    sha1_update(s, pad, 8);
    for (i = 0; i < 5; i++) {
        out[i*4] = (unsigned char)(s->h[i] >> 24);
        out[i*4+1] = (unsigned char)(s->h[i] >> 16);
        out[i*4+2] = (unsigned char)(s->h[i] >> 8);
        out[i*4+3] = (unsigned char)s->h[i];
    }
}

static const char b64tab[] = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
static void b64_encode(const unsigned char *in, size_t n, char *out) {
    size_t i, o = 0;
    for (i = 0; i + 2 < n; i += 3) {
        out[o++] = b64tab[(in[i] >> 2) & 63];
        out[o++] = b64tab[((in[i] & 3) << 4) | (in[i+1] >> 4)];
        out[o++] = b64tab[((in[i+1] & 15) << 2) | (in[i+2] >> 6)];
        out[o++] = b64tab[in[i+2] & 63];
    }
    if (i < n) {
        out[o++] = b64tab[(in[i] >> 2) & 63];
        if (i + 1 < n) {
            out[o++] = b64tab[((in[i] & 3) << 4) | (in[i+1] >> 4)];
            out[o++] = b64tab[(in[i+1] & 15) << 2];
            out[o++] = '=';
        } else {
            out[o++] = b64tab[(in[i] & 3) << 4];
            out[o++] = '=';
            out[o++] = '=';
        }
    }
    out[o] = '\0';
}

static void ws_accept_key(const char *key, char *out, size_t outcap) {
    static const char *guid = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11";
    Sha1 sh;
    unsigned char digest[20];
    char joined[256];
    snprintf(joined, sizeof(joined), "%s%s", key, guid);
    sha1_init(&sh);
    sha1_update(&sh, joined, strlen(joined));
    sha1_final(&sh, digest);
    (void)outcap;
    b64_encode(digest, 20, out);
}

/* ---------- handshake ---------- */
static char *ws_header_value(char *headers, const char *name) {
    char *p = headers;
    size_t n = strlen(name);
    while (p && *p) {
        char *e = strstr(p, "\r\n");
        if (!e) break;
        if ((size_t)(e - p) > n && p[n] == ':' &&
            strncmp(p, name, n) == 0) {
            char *v = p + n + 1;
            while (*v == ' ') v++;
            *e = '\0';
            return v;
        }
        p = e + 2;
    }
    return NULL;
}

static int ws_handshake(WsSocket fd, char **path_out) {
    char buf[16385];
    size_t len = 0;
    char *end, *line, *sp1, *sp2, *key, *accept;
    char resp[512];
    for (;;) {
        int k;
        if (len >= 16384) return -1;
        k = ws_recv(fd, buf + len, (int)(sizeof(buf) - 1 - len));
        if (k <= 0) return -1;
        len += (size_t)k;
        buf[len] = '\0';
        end = strstr(buf, "\r\n\r\n");
        if (end) break;
    }
    *end = '\0';
    line = buf;
    sp1 = strchr(line, ' ');
    sp2 = sp1 ? strchr(sp1 + 1, ' ') : NULL;
    if (!sp1 || !sp2) return -1;
    *sp1 = '\0';
    *sp2 = '\0';
    if (strcmp(line, "GET") != 0) return -1;
    *path_out = ws_dup(sp1 + 1);
    {
        char *h = strstr(sp2 + 1, "\r\n");
        if (!h) return -1;
        key = ws_header_value(h + 2, "Sec-WebSocket-Key");
    }
    if (!key) return -1;
    accept = (char *)malloc(64);
    if (!accept) well_panic("out of memory (WebSocket accept)");
    ws_accept_key(key, accept, 64);
    snprintf(resp, sizeof(resp),
             "HTTP/1.1 101 Switching Protocols\r\n"
             "Upgrade: websocket\r\nConnection: Upgrade\r\n"
             "Sec-WebSocket-Accept: %s\r\n\r\n", accept);
    free(accept);
    ws_send_all(fd, resp, strlen(resp));
    return 0;
}

/* ---------- frames ---------- */
static int ws_read_exact(WsSocket fd, unsigned char *p, size_t n) {
    while (n) {
        int k = ws_recv(fd, (char *)p, (int)n);
        if (k <= 0) return -1;
        p += k; n -= (size_t)k;
    }
    return 0;
}

static int ws_read_frame(WsSocket fd, int *opcode, unsigned char **payload, size_t *plen) {
    unsigned char h[2], mask[4];
    unsigned long long n;
    int masked;
    size_t i;
    if (ws_read_exact(fd, h, 2) != 0) return -1;
    *opcode = h[0] & 0x0F;
    if (!(h[0] & 0x80)) return -1; /* no fragmentation */
    masked = (h[1] & 0x80) != 0;
    n = h[1] & 0x7F;
    if (n == 126) {
        unsigned char e[2];
        if (ws_read_exact(fd, e, 2) != 0) return -1;
        n = ((unsigned)e[0] << 8) | e[1];
    } else if (n == 127) {
        unsigned char e[8];
        int j;
        if (ws_read_exact(fd, e, 8) != 0) return -1;
        n = 0;
        for (j = 0; j < 8; j++) n = (n << 8) | e[j];
    }
    if (!masked || n > 1048576ULL) return -1;
    if (ws_read_exact(fd, mask, 4) != 0) return -1;
    *payload = (unsigned char *)malloc((size_t)n + 1);
    if (!*payload) well_panic("out of memory (WebSocket frame)");
    if (ws_read_exact(fd, *payload, (size_t)n) != 0) { free(*payload); *payload = NULL; return -1; }
    for (i = 0; i < (size_t)n; i++) (*payload)[i] ^= mask[i & 3];
    (*payload)[n] = '\0';
    *plen = (size_t)n;
    return 0;
}

static void ws_send_frame(WsSocket fd, int opcode, const unsigned char *data, size_t n) {
    unsigned char h[10];
    size_t hn = 0;
    h[hn++] = (unsigned char)(0x80 | opcode);
    if (n < 126) {
        h[hn++] = (unsigned char)n;
    } else if (n <= 65535) {
        h[hn++] = 126;
        h[hn++] = (unsigned char)(n >> 8);
        h[hn++] = (unsigned char)n;
    } else {
        int i;
        h[hn++] = 127;
        for (i = 7; i >= 0; i--) h[hn++] = (unsigned char)((unsigned long long)n >> (i * 8));
    }
    ws_send_all(fd, (const char *)h, hn);
    if (n) ws_send_all(fd, (const char *)data, n);
}

void well_ws_send(long long client_id, const char *text) {
    if (client_id != g_ws_current_id || g_ws_current_fd == WS_INVALID_SOCKET || g_ws_current_closed) return;
    ws_send_frame(g_ws_current_fd, 0x1, (const unsigned char *)(text ? text : ""),
                  text ? strlen(text) : 0);
}

void well_ws_close(long long client_id, int code, const char *reason) {
    unsigned char p[128];
    size_t n = 0;
    if (client_id != g_ws_current_id || g_ws_current_closed) return;
    p[n++] = (unsigned char)(code >> 8);
    p[n++] = (unsigned char)code;
    if (reason) {
        size_t rl = strlen(reason);
        if (rl > 120) rl = 120;
        memcpy(p + n, reason, rl);
        n += rl;
    }
    ws_send_frame(g_ws_current_fd, 0x8, p, n);
    g_ws_current_closed = 1;
}

/* ---------- routes / dispatch ---------- */
static WsRoute *ws_find_route(const char *path) {
    WsRoute *r;
    well_mutex_lock(&g_ws_lock);
    for (r = g_ws_routes; r; r = r->next)
        if (strcmp(r->path, path) == 0) break;
    well_mutex_unlock(&g_ws_lock);
    return r;
}

void well_ws_route(const char *path, WellWsOpenFn open_fn,
                   WellWsMessageFn msg_fn, WellWsCloseFn close_fn) {
    WsRoute *r;
    if (!path) well_panic("Ws.route: null path");
    r = (WsRoute *)malloc(sizeof(WsRoute));
    if (!r) well_panic("out of memory (WebSocket route)");
    r->path = ws_dup(path);
    r->open_fn = open_fn;
    r->msg_fn = msg_fn;
    r->close_fn = close_fn;
    ws_lock_init();
    well_mutex_lock(&g_ws_lock);
    r->next = g_ws_routes;
    g_ws_routes = r;
    well_mutex_unlock(&g_ws_lock);
}

static int ws_run_open(WellWsOpenFn call, WellWsClient *c, long long oid) {
    WellCtx ctx;
    jmp_buf jb;
    jmp_buf *old;
    volatile int failed = 0;
    well_ctx_init(&ctx, oid);
    old = well_panic_push(&jb);
    if (setjmp(jb) == 0) {
        call(&ctx, c);
        well_panic_pop(old);
    } else {
        failed = 1;
        well_panic_pop(old);
    }
    well_ctx_clear(&ctx);
    return failed;
}

static int ws_run_close(WellWsCloseFn call, WellWsClient *c, int code,
                        WellString *reason, long long oid) {
    WellCtx ctx;
    jmp_buf jb;
    jmp_buf *old;
    volatile int failed = 0;
    well_ctx_init(&ctx, oid);
    old = well_panic_push(&jb);
    if (setjmp(jb) == 0) {
        call(&ctx, c, code, reason);
        well_panic_pop(old);
    } else {
        failed = 1;
        well_panic_pop(old);
    }
    well_ctx_clear(&ctx);
    return failed;
}

static void ws_serve_conn(WsSocket fd) {
    char *path = NULL;
    WsRoute *route;
    WellWsClient client;
    if (ws_handshake(fd, &path) != 0) { if (path) free(path); return; }
    route = ws_find_route(path);
    if (!route) {
        const char *nf = "HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n";
        ws_send_all(fd, nf, strlen(nf));
        free(path);
        return;
    }
    client.id = well_id_util_get_id();
    client.path = well_string_new(path);
    client.query = well_string_new("");
    free(path);
    g_ws_current_id = client.id;
    g_ws_current_fd = fd;
    g_ws_current_closed = 0;
    if (route->open_fn && ws_run_open(route->open_fn, &client, client.id)) {
        well_ws_close(client.id, 1011, "handler panic");
    }
    while (!g_ws_current_closed) {
        int opcode;
        unsigned char *payload = NULL;
        size_t plen = 0;
        if (ws_read_frame(fd, &opcode, &payload, &plen) != 0) break;
        if (opcode == 0x1) {
            WellString *text = well_string_new((const char *)payload);
            if (route->msg_fn) {
                WellCtx ctx;
                jmp_buf jb;
                jmp_buf *old;
                volatile int failed = 0;
                well_ctx_init(&ctx, client.id);
                old = well_panic_push(&jb);
                if (setjmp(jb) == 0) {
                    route->msg_fn(&ctx, &client, text);
                    well_panic_pop(old);
                } else {
                    failed = 1;
                    well_panic_pop(old);
                }
                well_ctx_clear(&ctx);
                if (failed) well_ws_close(client.id, 1011, "handler panic");
            }
            well_release(text);
        } else if (opcode == 0x8) {
            if (route->close_fn) {
                int code = 1000;
                WellString *reason = NULL;
                if (plen >= 2) {
                    code = (payload[0] << 8) | payload[1];
                    if (plen > 2) {
                        char *r = (char *)malloc(plen - 2 + 1);
                        if (r) { memcpy(r, payload + 2, plen - 2); r[plen-2] = '\0'; reason = well_string_new(r); free(r); }
                    }
                }
                (void)ws_run_close(route->close_fn, &client, code, reason, client.id);
                if (reason) well_release(reason);
            }
            break;
        } else if (opcode == 0x9) {
            ws_send_frame(fd, 0xA, payload, plen);
        } else if (opcode == 0xA) {
            /* pong */
        } else {
            free(payload);
            break;
        }
        free(payload);
    }
    ws_close_socket(fd);
    g_ws_current_fd = WS_INVALID_SOCKET;
    g_ws_current_id = 0;
    g_ws_current_closed = 1;
    well_release(client.path);
    well_release(client.query);
}

static void *ws_listener_thread(void *arg) {
    (void)arg;
    for (;;) {
        WsSocket s, fd;
        well_mutex_lock(&g_ws_lock);
        if (!g_ws_running) { well_mutex_unlock(&g_ws_lock); break; }
        fd = g_ws_listen_fd;
        well_mutex_unlock(&g_ws_lock);
        s = ws_accept(fd);
        if (s == WS_INVALID_SOCKET) break;
        ws_serve_conn(s);
    }
    well_mutex_lock(&g_ws_lock);
    g_ws_done = 1;
    well_cond_signal(&g_ws_cond);
    well_mutex_unlock(&g_ws_lock);
    return NULL;
}

void well_ws_listen(int port) {
    WsSockaddr addr;
    ws_lock_init();
    ws_load();
    if (g_ws_running) well_panic("Ws.listen: already running");
    memset(&addr, 0, sizeof(addr));
    addr.family = 2;
    addr.port = ws_htons((unsigned short)port);
    g_ws_listen_fd = ws_socket();
    if (g_ws_listen_fd == WS_INVALID_SOCKET) well_panic("Ws.listen: socket failed");
    if (ws_bind(g_ws_listen_fd, &addr) != 0) well_panic("Ws.listen: bind failed");
    if (ws_listen_socket(g_ws_listen_fd) != 0) well_panic("Ws.listen: listen failed");
    g_ws_running = 1; g_ws_done = 0;
    if (well_thread_start(&g_ws_thread, ws_listener_thread, NULL) != 0)
        well_panic("Ws.listen: cannot start thread");
    g_ws_thread_started = 1;
    well_mutex_lock(&g_ws_lock);
    while (!g_ws_done) well_cond_wait(&g_ws_cond, &g_ws_lock);
    well_mutex_unlock(&g_ws_lock);
    if (g_ws_thread_started) { well_thread_join(g_ws_thread); g_ws_thread_started = 0; }
    ws_close_socket(g_ws_listen_fd);
    g_ws_listen_fd = WS_INVALID_SOCKET;
    g_ws_running = 0;
}

void well_ws_stop(void) {
    WsSocket fd;
    ws_lock_init();
    well_mutex_lock(&g_ws_lock);
    g_ws_running = 0;
    fd = g_ws_listen_fd;
    g_ws_listen_fd = WS_INVALID_SOCKET;
    well_cond_signal(&g_ws_cond);
    well_mutex_unlock(&g_ws_lock);
    ws_close_socket(fd);
}

void well_ws_shutdown(void) {
    if (g_ws_running) well_ws_stop();
}

WellModule well_ws_module = { "ws", NULL, NULL, NULL, well_ws_shutdown };

