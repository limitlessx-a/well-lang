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
static int (*pshutdown)(WsSocket, int);
static int (*psetsockopt)(WsSocket, int, int, const char *, int);
static int (*pselect)(int, void *, void *, void *, const void *);
static int (*pclosesocket)(WsSocket);
#define WS_SOL_SOCKET 0xFFFF
#define WS_SO_RCVTIMEO 0x1006
#define WS_SO_SNDTIMEO 0x1005
/* Winsock fd_set/timeval layouts (ws2_32 is resolved dynamically). */
typedef struct { unsigned int count; WsSocket fds[64]; } WsFdSet;
typedef struct { long sec; long usec; } WsTimeval;
#else
#include <sys/socket.h>
#include <sys/time.h>
#include <netinet/in.h>
#include <unistd.h>
#include <signal.h>
#include <poll.h>
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
static WsSocket g_ws_listen_fd = WS_INVALID_SOCKET;
static WellThread g_ws_thread;
static int g_ws_thread_started = 0;

/* Audit W2: blocking recv/send without a deadline lets one half-open or idle
 * peer pin a reader thread forever, and there was no cap on live connections
 * (one thread per connection, so the cap keeps thread/memory use bounded). */
#define WS_HANDSHAKE_TIMEOUT_MS 10000 /* client must finish the upgrade in 10s */
#define WS_IDLE_TIMEOUT_MS 60000     /* close a connection with no frame for 60s */
#define WS_SEND_TIMEOUT_MS 15000     /* give up writing to a stalled peer */
/* The #ifndef lets a test build lower the cap with -DWS_MAX_CONNECTIONS=N. */
#ifndef WS_MAX_CONNECTIONS
#define WS_MAX_CONNECTIONS 4096
#endif

/* One entry per live connection; guarded by g_ws_lock. */
typedef struct WsConn {
    long long id;
    WsSocket fd;
    WsRoute *route;
    WellWsClient client;
    int closed;
    int close_requested;
    int close_code;
    WellString *close_reason;
    int refs; /* list reference + one per scheduled task */
    long long idle_deadline; /* frame-idle deadline checked by the reader (W2) */
    /* Audit W3: both the handler thread (Ws.send/close) and the reader thread
     * (pong/close echo) write frames to the same socket. Every frame write is
     * serialized by this per-connection lock so headers/payloads cannot
     * interleave with a frame from the other thread. */
    WellMutex write_lock;
    struct WsConn *next;
} WsConn;

static WsConn *g_ws_conns = NULL;
static int g_ws_conn_count = 0; /* live WsConn entries, guarded by g_ws_lock */

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
    pshutdown = (int (*)(WsSocket, int))GetProcAddress(g_wsm, "shutdown");
    psetsockopt = (int (*)(WsSocket, int, int, const char *, int))GetProcAddress(g_wsm, "setsockopt");
    pselect = (int (*)(int, void *, void *, void *, const void *))GetProcAddress(g_wsm, "select");
    pclosesocket = (int (*)(WsSocket))GetProcAddress(g_wsm, "closesocket");
    if (!pWSAStartup || !psocket || !pbind || !plisten || !paccept ||
        !precv || !psend || !pshutdown || !psetsockopt || !pselect || !pclosesocket)
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
static void ws_shutdown_socket(WsSocket s) { if (s != WS_INVALID_SOCKET) pshutdown(s, 2); }
/* Audit W7: Ws.close() wakes the reader (blocked in recv) without discarding
 * frames that are already queued for the peer. SD_RECEIVE = 0. */
static void ws_shutdown_read(WsSocket s) { if (s != WS_INVALID_SOCKET) pshutdown(s, 0); }
/* SD_SEND = 1: flush queued frames, then FIN (used before draining on a
 * protocol error so close() does not turn into RST). */
static void ws_shutdown_write(WsSocket s) { if (s != WS_INVALID_SOCKET) pshutdown(s, 1); }
/* Accept timeout plus read/write timeouts on the accepted socket. */
static void ws_apply_timeouts(WsSocket s, int recv_ms, int send_ms) {
    DWORD rv = (DWORD)recv_ms;
    DWORD sv = (DWORD)send_ms;
    psetsockopt(s, WS_SOL_SOCKET, WS_SO_RCVTIMEO, (const char *)&rv, (int)sizeof(rv));
    psetsockopt(s, WS_SOL_SOCKET, WS_SO_SNDTIMEO, (const char *)&sv, (int)sizeof(sv));
}
/* Windows allows rebinding a TIME_WAIT port by default; SO_REUSEADDR there
 * also permits hijacking an active listener, so leave it alone. */
static void ws_set_reuseaddr(WsSocket s) { (void)s; }
/* Windows closesocket() unblocks a blocked accept(), so blocking accept is
 * fine there; POSIX needs the poll-based helper below. The frame reader also
 * uses this helper to notice Ws.close()/idle timeouts (audit W7). */
static int ws_wait_readable(WsSocket s, int ms) {
    WsFdSet rd;
    WsTimeval tv;
    int r;
    if (s == WS_INVALID_SOCKET) return 0;
    memset(&rd, 0, sizeof(rd));
    rd.count = 1;
    rd.fds[0] = s;
    tv.sec = ms / 1000;
    tv.usec = (long)(ms % 1000) * 1000;
    r = pselect(0, &rd, NULL, NULL, &tv);
    return r > 0 && rd.count > 0;
}
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
static void ws_shutdown_socket(WsSocket s) { if (s >= 0) shutdown(s, SHUT_RDWR); }
/* Audit W7: see the Windows branch. */
static void ws_shutdown_read(WsSocket s) { if (s >= 0) shutdown(s, SHUT_RD); }
/* See the Windows branch (used before draining on a protocol error). */
static void ws_shutdown_write(WsSocket s) { if (s >= 0) shutdown(s, SHUT_WR); }
/* Accept timeout plus read/write timeouts on the accepted socket. */
static void ws_apply_timeouts(WsSocket s, int recv_ms, int send_ms) {
    struct timeval tv;
    tv.tv_sec = send_ms / 1000;
    tv.tv_usec = (send_ms % 1000) * 1000;
    setsockopt(s, SOL_SOCKET, SO_SNDTIMEO, (const char *)&tv, (int)sizeof(tv));
    tv.tv_sec = recv_ms / 1000;
    tv.tv_usec = (recv_ms % 1000) * 1000;
    setsockopt(s, SOL_SOCKET, SO_RCVTIMEO, (const char *)&tv, (int)sizeof(tv));
}
/* Let a server restart immediately even when the old listener's connections
 * are still in TIME_WAIT (the tests reuse ports across runs). */
static void ws_set_reuseaddr(WsSocket s) {
    int one = 1;
    setsockopt(s, SOL_SOCKET, SO_REUSEADDR, (const char *)&one, sizeof(one));
}
/* Linux does not wake a blocked accept() when the listening fd is closed from
 * another thread, so poll with a short timeout and re-check the running flag. */
static int ws_wait_readable(WsSocket s, int ms) {
    struct pollfd pfd;
    int r;
    pfd.fd = s;
    pfd.events = POLLIN;
    pfd.revents = 0;
    r = poll(&pfd, 1, ms);
    return r > 0 && (pfd.revents & POLLIN) != 0;
}
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
/* RFC 7230: header field names are case-insensitive. */
static int ws_ieq(const char *a, const char *b, size_t n) {
    size_t i;
    for (i = 0; i < n; i++) {
        char x = a[i], y = b[i];
        if (x >= 'A' && x <= 'Z') x = (char)(x - 'A' + 'a');
        if (y >= 'A' && y <= 'Z') y = (char)(y - 'A' + 'a');
        if (x != y) return 0;
    }
    return 1;
}

/* The handshake block is NUL-terminated in place of the final CRLF (see
 * ws_handshake), so the last header line ends at the string terminator rather
 * than at "\r\n"; accepting that made the final header line invisible (W1). */
static char *ws_header_value(char *headers, const char *name) {
    char *p = headers;
    size_t n = strlen(name);
    while (p && *p) {
        char *e = strstr(p, "\r\n");
        if (!e) e = p + strlen(p);
        if ((size_t)(e - p) > n && p[n] == ':' && ws_ieq(p, name, n)) {
            char *v = p + n + 1;
            char *ve = e;
            while (v < ve && (*v == ' ' || *v == '\t')) v++;
            while (ve > v && (ve[-1] == ' ' || ve[-1] == '\t')) ve--;
            *ve = '\0';
            return v;
        }
        if (!*e) break;
        p = e + 2;
    }
    return NULL;
}

/* Reads and parses the upgrade request. On success fills *path_out (heap,
 * still containing any "?query" suffix; the caller splits it, audit W8) and
 * `accept` with the Sec-WebSocket-Accept value. Nothing is written to the
 * socket here: the caller validates the route first so an unknown path gets a
 * plain HTTP 404 instead of an upgrade followed by a 404 (audit W4). */
static int ws_handshake_read(WsSocket fd, char **path_out, char *accept, size_t accept_cap) {
    char buf[16385];
    size_t len = 0;
    char *end, *line, *sp1, *sp2, *key;
    long long deadline = well_host_now_ms() + WS_HANDSHAKE_TIMEOUT_MS;
    for (;;) {
        int k;
        if (len >= 16384) return -1;
        if (well_host_now_ms() >= deadline) return -1; /* header deadline (W2) */
        k = ws_recv(fd, buf + len, (int)(sizeof(buf) - 1 - len));
        if (k <= 0) return -1;
        len += (size_t)k;
        buf[len] = '\0';
        end = strstr(buf, "\r\n\r\n");
        if (end) break;
    }
    /* In-place NUL instead of the final CRLF; ws_header_value treats the string
     * terminator as the end of the last header line. */
    *end = '\0';
    line = buf;
    sp1 = strchr(line, ' ');
    sp2 = sp1 ? strchr(sp1 + 1, ' ') : NULL;
    if (!sp1 || !sp2) return -1;
    *sp1 = '\0';
    *sp2 = '\0';
    if (strcmp(line, "GET") != 0) return -1;
    key = NULL;
    {
        char *h = strstr(sp2 + 1, "\r\n");
        if (!h) return -1;
        key = ws_header_value(h + 2, "Sec-WebSocket-Key");
    }
    if (!key) return -1;
    *path_out = ws_dup(sp1 + 1);
    if (!*path_out) well_panic("out of memory (WebSocket path)");
    ws_accept_key(key, accept, accept_cap);
    return 0;
}

/* Sends the 101 response; called only after the route resolved (audit W4). */
static int ws_handshake_send(WsSocket fd, const char *accept) {
    char resp[512];
    snprintf(resp, sizeof(resp),
             "HTTP/1.1 101 Switching Protocols\r\n"
             "Upgrade: websocket\r\nConnection: Upgrade\r\n"
             "Sec-WebSocket-Accept: %s\r\n\r\n", accept);
    return ws_send_all(fd, resp, strlen(resp));
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

/* Audit W7: frame reads wait in 200ms slices so a concurrent Ws.close() (or
 * the frame-idle deadline, W2) is noticed even while the reader is blocked in
 * recv(); a plain blocking recv() kept the thread and socket alive until the
 * peer eventually answered or the socket timeout expired. */
static int ws_read_exact_frame(WsConn *c, WsSocket fd, unsigned char *p, size_t n) {
    while (n) {
        int k;
        if (!ws_wait_readable(fd, 200)) {
            int closed;
            long long deadline;
            well_mutex_lock(&g_ws_lock);
            closed = c->closed;
            deadline = c->idle_deadline;
            well_mutex_unlock(&g_ws_lock);
            if (closed) return -1;
            if (deadline > 0 && well_host_now_ms() >= deadline) return -1;
            continue;
        }
        k = ws_recv(fd, (char *)p, (int)n);
        if (k <= 0) return -1;
        p += k; n -= (size_t)k;
    }
    return 0;
}

/* Close codes used for protocol rejections (audit W6/W9). */
#define WS_CLOSE_PROTOCOL 1002
#define WS_CLOSE_UNSUPPORTED 1003
#define WS_CLOSE_TOO_BIG 1009
#define WS_MAX_FRAME_BYTES 1048576ULL

/* Reads one frame. Returns 0 on success, -1 on a fatal I/O error or a protocol
 * violation; in the latter case *close_code is set so the caller can send a
 * proper close frame before dropping the connection (W6: the old code just
 * closed the socket, so the peer could not tell 1002/1003/1009 apart). */
static int ws_read_frame(WsConn *c, WsSocket fd, int *opcode, unsigned char **payload,
                         size_t *plen, int *close_code) {
    unsigned char h[2], mask[4];
    unsigned long long n;
    int masked, fin;
    size_t i;
    *close_code = 0;
    if (ws_read_exact_frame(c, fd, h, 2) != 0) return -1;
    *opcode = h[0] & 0x0F;
    fin = (h[0] & 0x80) != 0;
    if (h[0] & 0x70) { *close_code = WS_CLOSE_PROTOCOL; return -1; } /* W9: RSV */
    masked = (h[1] & 0x80) != 0;
    if (!masked) { *close_code = WS_CLOSE_PROTOCOL; return -1; }
    n = h[1] & 0x7F;
    if (n == 126) {
        unsigned char e[2];
        if (ws_read_exact_frame(c, fd, e, 2) != 0) return -1;
        n = ((unsigned)e[0] << 8) | e[1];
    } else if (n == 127) {
        unsigned char e[8];
        int j;
        if (ws_read_exact_frame(c, fd, e, 8) != 0) return -1;
        n = 0;
        for (j = 0; j < 8; j++) n = (n << 8) | e[j];
    }
    if (*opcode >= 0x8) {
        /* Control frames must not be fragmented and carry at most 125 bytes. */
        if (!fin || n > 125) { *close_code = WS_CLOSE_PROTOCOL; return -1; }
        if (*opcode != 0x8 && *opcode != 0x9 && *opcode != 0xA) {
            *close_code = WS_CLOSE_PROTOCOL;
            return -1;
        }
    } else {
        /* Fragmentation and binary payloads are not implemented; tell the peer
         * instead of silently dropping or resetting the connection. */
        if (!fin) { *close_code = WS_CLOSE_UNSUPPORTED; return -1; }
        if (*opcode != 0x1 && *opcode != 0x2) { *close_code = WS_CLOSE_PROTOCOL; return -1; }
    }
    if (n > WS_MAX_FRAME_BYTES) { *close_code = WS_CLOSE_TOO_BIG; return -1; }
    if (ws_read_exact_frame(c, fd, mask, 4) != 0) return -1;
    *payload = (unsigned char *)malloc((size_t)n + 1);
    if (!*payload) well_panic("out of memory (WebSocket frame)");
    if (ws_read_exact_frame(c, fd, *payload, (size_t)n) != 0) { free(*payload); *payload = NULL; return -1; }
    for (i = 0; i < (size_t)n; i++) (*payload)[i] ^= mask[i & 3];
    (*payload)[n] = '\0';
    *plen = (size_t)n;
    return 0;
}

/* Audit W6: before closing after a protocol error, send FIN and consume what
 * the peer already queued. Without this, close() on a socket with unread data
 * makes TCP emit RST, which can discard the close frame we just sent. */
static void ws_drain_and_shutdown(WsSocket fd) {
    char tmp[512];
    int i;
    if (fd == WS_INVALID_SOCKET) return;
    ws_shutdown_write(fd);
    for (i = 0; i < 40; i++) {
        if (!ws_wait_readable(fd, 25)) break;
        if (ws_recv(fd, tmp, (int)sizeof(tmp)) <= 0) break;
    }
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

/* Single write entry point for one connection (audit W3): the handler thread
 * (Ws.send / Ws.close) and the reader thread (pong / close echo) both come
 * through here, and the per-connection write lock keeps a frame's header and
 * payload from interleaving with another frame. The caller must hold a
 * reference to `c` so it cannot be freed while the send is in flight.
 * Lock order is write_lock -> g_ws_lock (never the reverse): the fd is read
 * under both locks so a concurrent close cannot hand us a stale/reused fd. */
static void ws_conn_write_frame(WsConn *c, int opcode, const unsigned char *data, size_t n) {
    WsSocket fd;
    well_mutex_lock(&c->write_lock);
    well_mutex_lock(&g_ws_lock);
    fd = c->fd;
    if (c->closed || fd == WS_INVALID_SOCKET) {
        well_mutex_unlock(&g_ws_lock);
        well_mutex_unlock(&c->write_lock);
        return;
    }
    well_mutex_unlock(&g_ws_lock);
    /* g_ws_lock is not held while send() may block: one slow client must not
     * stall Ws.send for every other connection. */
    ws_send_frame(fd, opcode, data, n);
    well_mutex_unlock(&c->write_lock);
}

/* Sends a close frame with a status code through the serialized frame writer
 * (audit W6: protocol errors get a proper close frame before the socket goes
 * away). */
static void ws_send_close_code(WsConn *c, int code) {
    unsigned char p[2];
    p[0] = (unsigned char)(code >> 8);
    p[1] = (unsigned char)code;
    ws_conn_write_frame(c, 0x8, p, 2);
}

/* ---------- connection lifetime (all refcount ops hold g_ws_lock) ---------- */

static WsConn *ws_conn_find_locked(long long id) {
    WsConn *c;
    for (c = g_ws_conns; c; c = c->next)
        if (c->id == id) return c;
    return NULL;
}

static void ws_conn_free(WsConn *c) {
    well_mutex_destroy(&c->write_lock);
    if (c->client.path) well_release(c->client.path);
    if (c->client.query) well_release(c->client.query);
    if (c->close_reason) well_release(c->close_reason);
    free(c);
}

/* Caller holds g_ws_lock. Unlinks on the last ref; returns the orphan to free. */
static WsConn *ws_conn_unref_locked(WsConn *c) {
    WsConn **pp;
    c->refs--;
    if (c->refs > 0) return NULL;
    pp = &g_ws_conns;
    while (*pp) {
        if (*pp == c) {
            *pp = c->next;
            g_ws_conn_count--;
            break;
        }
        pp = &(*pp)->next;
    }
    return c;
}

static void ws_conn_unref(WsConn *c) {
    WsConn *dead;
    well_mutex_lock(&g_ws_lock);
    dead = ws_conn_unref_locked(c);
    well_mutex_unlock(&g_ws_lock);
    if (dead) ws_conn_free(dead);
}

void well_ws_send(long long client_id, const char *text) {
    WsConn *c;
    ws_lock_init();
    well_mutex_lock(&g_ws_lock);
    c = ws_conn_find_locked(client_id);
    if (c) c->refs++; /* keep it alive / out of the free path while sending */
    well_mutex_unlock(&g_ws_lock);
    if (!c) return;
    ws_conn_write_frame(c, 0x1, (const unsigned char *)(text ? text : ""),
                        text ? strlen(text) : 0);
    ws_conn_unref(c);
}

void well_ws_close(long long client_id, int code, const char *reason) {
    unsigned char p[128];
    WsConn *c;
    WsSocket fd = WS_INVALID_SOCKET;
    size_t n = 0;
    ws_lock_init();
    well_mutex_lock(&g_ws_lock);
    c = ws_conn_find_locked(client_id);
    if (c) c->refs++;
    well_mutex_unlock(&g_ws_lock);
    if (!c) return;
    p[n++] = (unsigned char)(code >> 8);
    p[n++] = (unsigned char)code;
    if (reason) {
        size_t rl = strlen(reason);
        if (rl > 120) rl = 120;
        memcpy(p + n, reason, rl);
        n += rl;
    }
    ws_conn_write_frame(c, 0x8, p, n);
    well_mutex_lock(&g_ws_lock);
    if (!c->closed) {
        c->closed = 1;
        c->close_requested = 1;
        c->close_code = code;
        fd = c->fd;
        if (c->close_reason) well_release(c->close_reason);
        c->close_reason = well_string_new(reason ? reason : "");
    }
    well_mutex_unlock(&g_ws_lock);
    /* Audit W7: wake the reader blocked in recv() so the connection and its
     * thread are released (and the close handler fires) even when the client
     * never answers the close handshake. Shutting down only the read side
     * keeps the close frame that was just queued for delivery. */
    if (fd != WS_INVALID_SOCKET) ws_shutdown_read(fd);
    ws_conn_unref(c);
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

/* ---------- handler dispatch: one task per event, serialized per client oid ---------- */

typedef struct WsTask {
    WsConn *conn;
    int kind; /* 0 open, 1 message, 2 close */
    WellString *text;
    int code;
    WellString *reason;
} WsTask;

static void ws_task_run(WellCtx *ctx, void *arg) {
    WsTask *t = (WsTask *)arg;
    WsRoute *r = t->conn->route;
    if (!r) return;
    if (t->kind == 0) {
        if (r->open_fn) r->open_fn(ctx, &t->conn->client);
    } else if (t->kind == 1) {
        if (r->msg_fn) r->msg_fn(ctx, &t->conn->client, t->text);
    } else {
        if (r->close_fn) r->close_fn(ctx, &t->conn->client, t->code, t->reason);
    }
}

static void ws_task_done(void *arg) {
    WsTask *t = (WsTask *)arg;
    WsConn *c = t->conn;
    if (t->text) well_release(t->text);
    if (t->reason) well_release(t->reason);
    free(t);
    ws_conn_unref(c);
}

/* Handler panics are isolated by the scheduler (setjmp in dispatch_task). */
static void ws_dispatch(WsConn *c, int kind, WellString *text, int code,
                        WellString *reason) {
    WsTask *t = (WsTask *)calloc(1, sizeof(WsTask));
    if (!t) well_panic("out of memory (WebSocket task)");
    t->conn = c;
    t->kind = kind;
    t->text = text;
    t->code = code;
    t->reason = reason;
    well_mutex_lock(&g_ws_lock);
    c->refs++;
    well_mutex_unlock(&g_ws_lock);
    well_schedule_task(c->id, ws_task_run, t, ws_task_done);
}

static void *ws_reader_thread(void *arg) {
    WsConn *c = (WsConn *)arg;
    WsSocket fd;
    char *path = NULL;
    char *query = NULL;
    char accept[64];
    WsRoute *route = NULL;
    int want_close = 0;
    int code = 1006;
    WellString *reason = NULL;

    well_mutex_lock(&g_ws_lock);
    fd = c->fd;
    well_mutex_unlock(&g_ws_lock);

    if (ws_handshake_read(fd, &path, accept, sizeof(accept)) != 0) goto finish;
    /* Audit W8: match the route on the path only and pass the query string to
     * the open handler instead of dropping it / breaking the match. */
    query = strchr(path, '?');
    if (query) {
        *query = '\0';
        query++;
    }
    route = ws_find_route(path);
    if (!route) {
        const char *nf = "HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n";
        ws_send_all(fd, nf, strlen(nf));
        goto finish;
    }
    /* Audit W4: only upgrade after the route is known to exist. */
    if (ws_handshake_send(fd, accept) != 0) goto finish;
    /* Upgrade complete: switch from the handshake deadline to the idle-frame
     * deadline (W2). */
    ws_apply_timeouts(fd, WS_IDLE_TIMEOUT_MS, WS_SEND_TIMEOUT_MS);
    well_mutex_lock(&g_ws_lock);
    c->route = route;
    c->idle_deadline = well_host_now_ms() + WS_IDLE_TIMEOUT_MS;
    c->client.path = well_string_new(path);
    c->client.query = well_string_new(query ? query : "");
    well_mutex_unlock(&g_ws_lock);
    ws_dispatch(c, 0, NULL, 0, NULL);

    for (;;) {
        int opcode;
        int close_code = 0;
        unsigned char *payload = NULL;
        size_t plen = 0;
        if (ws_read_frame(c, fd, &opcode, &payload, &plen, &close_code) != 0) {
            /* W6/W9: send the matching close code (1002/1003/1009) instead of
             * dropping the TCP connection without a word; then drain so the
             * close frame is not discarded by an RST. */
            if (close_code) {
                ws_send_close_code(c, close_code);
                ws_drain_and_shutdown(fd);
            }
            break;
        }
        /* Any received frame resets the frame-idle deadline (audit W2). */
        well_mutex_lock(&g_ws_lock);
        c->idle_deadline = well_host_now_ms() + WS_IDLE_TIMEOUT_MS;
        well_mutex_unlock(&g_ws_lock);
        if (opcode == 0x1) {
            WellString *text = well_string_new((const char *)payload);
            ws_dispatch(c, 1, text, 0, NULL);
        } else if (opcode == 0x2) {
            /* Audit W5: the language API has no binary message, so answer with
             * 1003 (unsupported data) instead of silently dropping it. */
            ws_send_close_code(c, WS_CLOSE_UNSUPPORTED);
            ws_drain_and_shutdown(fd);
            free(payload);
            want_close = 1;
            code = WS_CLOSE_UNSUPPORTED;
            break;
        } else if (opcode == 0x8) {
            if (plen == 1) { /* invalid close payload (W6) */
                ws_send_close_code(c, WS_CLOSE_PROTOCOL);
                ws_drain_and_shutdown(fd);
                free(payload);
                break;
            }
            want_close = 1;
            code = 1000;
            if (plen >= 2) {
                code = (payload[0] << 8) | payload[1];
                if (plen > 2) {
                    char *r = (char *)malloc(plen - 2 + 1);
                    if (r) {
                        memcpy(r, payload + 2, plen - 2);
                        r[plen - 2] = '\0';
                        reason = well_string_new(r);
                        free(r);
                    }
                }
            }
            /* The reader thread must use the same write lock as Ws.send so the
             * close echo cannot split a handler frame (W3). */
            ws_conn_write_frame(c, 0x8, payload, plen);
            free(payload);
            break;
        } else if (opcode == 0x9) {
            ws_conn_write_frame(c, 0xA, payload, plen);
        }
        free(payload);
    }

finish:
    {
        WsSocket to_close;
        /* Same lock order as ws_conn_write_frame (write_lock -> g_ws_lock) so
         * no frame write can be in flight on the fd we are about to close. */
        well_mutex_lock(&c->write_lock);
        well_mutex_lock(&g_ws_lock);
        c->closed = 1;
        if (c->close_requested && !want_close) {
            want_close = 1;
            code = c->close_code;
            reason = c->close_reason;
            c->close_reason = NULL;
            c->close_requested = 0;
        }
        to_close = c->fd;
        c->fd = WS_INVALID_SOCKET;
        well_mutex_unlock(&g_ws_lock);
        well_mutex_unlock(&c->write_lock);
        if (to_close != WS_INVALID_SOCKET) ws_close_socket(to_close);
        if (route && route->close_fn) ws_dispatch(c, 2, NULL, code, reason);
        else if (reason) well_release(reason);
    }
    free(path);
    ws_conn_unref(c); /* list reference; the close task keeps it alive if scheduled */
    well_service_release();
    return NULL;
}

static void *ws_listener_thread(void *arg) {
    WsSocket fd = WS_INVALID_SOCKET;
    (void)arg;
    for (;;) {
        WsSocket s;
        WsConn *c;
        WellThread th;
        well_mutex_lock(&g_ws_lock);
        if (!g_ws_running) {
            well_mutex_unlock(&g_ws_lock);
            break;
        }
        fd = g_ws_listen_fd;
        well_mutex_unlock(&g_ws_lock);
        if (!ws_wait_readable(fd, 100)) continue;
        s = ws_accept(fd);
        if (s == WS_INVALID_SOCKET) break;
        ws_apply_timeouts(s, WS_HANDSHAKE_TIMEOUT_MS, WS_SEND_TIMEOUT_MS);
        {   /* Refuse instead of spawning a thread past the cap (W2). */
            int at_cap;
            well_mutex_lock(&g_ws_lock);
            at_cap = g_ws_conn_count >= WS_MAX_CONNECTIONS;
            well_mutex_unlock(&g_ws_lock);
            if (at_cap) {
                static const char busy[] =
                    "HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\n"
                    "Connection: close\r\nRetry-After: 1\r\n\r\n";
                ws_send_all(s, busy, sizeof(busy) - 1);
                ws_close_socket(s);
                continue;
            }
        }
        c = (WsConn *)calloc(1, sizeof(WsConn));
        if (!c) well_panic("out of memory (WebSocket connection)");
        c->id = well_id_util_get_id();
        c->client.id = c->id;
        c->fd = s;
        c->refs = 1; /* list reference */
        well_mutex_init(&c->write_lock); /* per-connection frame write lock (W3) */
        well_service_hold(); /* keeps the run loop alive while the client is connected */
        well_mutex_lock(&g_ws_lock);
        c->next = g_ws_conns;
        g_ws_conns = c;
        g_ws_conn_count++;
        well_mutex_unlock(&g_ws_lock);
        if (well_thread_start(&th, ws_reader_thread, c) != 0)
            well_panic("WebSocket: cannot start reader thread");
        well_thread_detach(th);
    }
    well_mutex_lock(&g_ws_lock);
    g_ws_running = 0;
    fd = g_ws_listen_fd;
    g_ws_listen_fd = WS_INVALID_SOCKET;
    well_mutex_unlock(&g_ws_lock);
    if (fd != WS_INVALID_SOCKET) ws_close_socket(fd);
    well_service_release();
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
    ws_set_reuseaddr(g_ws_listen_fd);
    if (ws_bind(g_ws_listen_fd, &addr) != 0) well_panic("Ws.listen: bind failed");
    if (ws_listen_socket(g_ws_listen_fd) != 0) well_panic("Ws.listen: listen failed");
    g_ws_running = 1;
    if (well_thread_start(&g_ws_thread, ws_listener_thread, NULL) != 0)
        well_panic("Ws.listen: cannot start thread");
    g_ws_thread_started = 1;
    well_service_hold();
    /* 非阻塞：listener 与 worker 生命周期由 well_run_loop 管理。 */
}

void well_ws_stop(void) {
    WsSocket fd;
    WsConn *c;
    ws_lock_init();
    well_mutex_lock(&g_ws_lock);
    g_ws_running = 0;
    fd = g_ws_listen_fd;
    g_ws_listen_fd = WS_INVALID_SOCKET;
    for (c = g_ws_conns; c; c = c->next) {
        c->closed = 1;
        /* shutdown(), not close(): the reader thread is blocked in recv() and
         * owns the socket. Closing it from here aborts the connection with RST
         * and can discard responses that were just sent. */
        if (c->fd != WS_INVALID_SOCKET) ws_shutdown_socket(c->fd);
    }
    well_cond_signal(&g_ws_cond);
    well_mutex_unlock(&g_ws_lock);
    if (fd != WS_INVALID_SOCKET) ws_close_socket(fd);
}

void well_ws_shutdown(void) {
    WsConn *c;
    if (g_ws_running) well_ws_stop();
    if (g_ws_thread_started) {
        well_thread_join(g_ws_thread);
        g_ws_thread_started = 0;
    }
    /* Each connection holds a service count, so the run loop can only return
     * after every reader thread has exited; close leftovers defensively. */
    well_mutex_lock(&g_ws_lock);
    for (c = g_ws_conns; c; c = c->next) {
        if (c->fd != WS_INVALID_SOCKET) {
            WsSocket f = c->fd;
            c->fd = WS_INVALID_SOCKET;
            ws_close_socket(f);
        }
    }
    well_mutex_unlock(&g_ws_lock);
}

WellModule well_ws_module = { "ws", NULL, NULL, NULL, well_ws_shutdown };