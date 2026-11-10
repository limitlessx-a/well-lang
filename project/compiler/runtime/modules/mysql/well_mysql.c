#include "wellrt.h"
#include "well_mysql.h"
#include "well_internal.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* Audit M5: MySQL I/O used to block forever. Every socket now gets an
 * SO_RCVTIMEO/SO_SNDTIMEO budget and connect() gets an explicit deadline, so a
 * half-open connection, a network black hole or a malicious server can no
 * longer pin a worker thread indefinitely. */
#define MYSQL_CONNECT_TIMEOUT_MS 5000
#define MYSQL_IO_TIMEOUT_MS 15000

#ifdef _WIN32
#include <windows.h>
typedef unsigned long long MysqlSocket;
/* 16-byte AF_INET sockaddr: family, port, addr, padding. `unsigned int` (not
 * `unsigned long`) keeps the layout identical to sockaddr_in on every ABI. */
typedef struct { unsigned short family, port; unsigned int addr; char zero[8]; } MysqlAddr;
#define MYSQL_INVALID_SOCKET (~0ULL)
static HMODULE g_mym;
static int (*mWSAStartup)(int, void *);
static int (*mWSAGetLastError)(void);
static MysqlSocket (*msocket)(int, int, int);
static int (*mbind)(MysqlSocket, const void *, int);
static MysqlSocket (*mconnect)(MysqlSocket, const void *, int);
static int (*mrecv)(MysqlSocket, char *, int, int);
static int (*msend)(MysqlSocket, const char *, int, int);
static int (*mselect)(int, void *, void *, void *, const void *);
static int (*msetsockopt)(MysqlSocket, int, int, const char *, int);
static int (*mgetsockopt)(MysqlSocket, int, int, char *, int *);
static int (*mioctlsocket)(MysqlSocket, long, unsigned long *);
static int (*mshutdown)(MysqlSocket, int);
static int (*mclosesocket)(MysqlSocket);
#define MYSQL_SOL_SOCKET 0xFFFF
#define MYSQL_SO_RCVTIMEO 0x1006
#define MYSQL_SO_SNDTIMEO 0x1005
#define MYSQL_SO_ERROR 0x1007
#define MYSQL_FIONBIO ((long)0x8004667EL)
#define MYSQL_WSAEWOULDBLOCK 10035
#define MYSQL_WSAETIMEDOUT 10060
/* Winsock fd_set/timeval layouts (the module resolves ws2_32 dynamically, so
 * <winsock2.h> is not included). FD_SETSIZE is 64 in Winsock. */
typedef struct { unsigned int count; MysqlSocket fds[64]; } MysqlFdSet;
typedef struct { long sec; long usec; } MysqlTimeval;
#else
#include <sys/socket.h>
#include <sys/time.h>
#include <netinet/in.h>
#include <unistd.h>
#include <signal.h>
#include <poll.h>
#include <fcntl.h>
#include <errno.h>
typedef int MysqlSocket;
typedef struct { unsigned short family, port; unsigned int addr; char zero[8]; } MysqlAddr;
#define MYSQL_INVALID_SOCKET (-1)
#endif

typedef struct WellMysqlConn {
    MysqlSocket fd;
    unsigned char seq;
    int connected;
    int checked_out; /* currently borrowed from a pool (audit M12) */
    int io_err; /* last low-level I/O outcome, for explicit panic messages */
    long long last_used_ms; /* last acquire/release: avoids a ping per query */
    /* v3 lesson (P1): small reads come out of an 8 KiB buffer so a scalar
     * query costs one recv instead of one syscall per protocol field.  Large
     * reads bypass the buffer and land directly in the destination. */
    char read_buf[8192];
    int read_pos;
    int read_end;
    struct WellMysqlConn *next_all; /* pool's live-connection list */
} WellMysqlConn;

#define MY_IO_OK 0
#define MY_IO_ERROR 1
#define MY_IO_TIMEOUT 2
#define MY_IO_EOF 3

static WellMysqlConn g_default;

/* Pool acquire used to send COM_PING on every borrow, doubling the round trips
 * for a trivial query.  A connection that was used within this window is known
 * alive, so only ping after it has been idle for a while.  0 = always ping
 * (old behaviour), <0 = never ping. */
static long long g_ping_idle_ms = 1000;
static int g_ping_cfg = 0;

static void my_ping_cfg_init(void) {
    const char *e;
    if (g_ping_cfg) return;
    g_ping_cfg = 1;
    e = getenv("WELL_MYSQL_PING_IDLE_MS");
    if (e && e[0]) g_ping_idle_ms = atoll(e);
}

/* Audit M11: the process-wide default connection used to be completely
 * unsynchronized, so two handlers calling Mysql.exec/queryJson concurrently
 * interleaved the wire protocol on one socket. Serialize it and release the
 * lock on the panic path (see well_mysql_exec below). */
static WellMutex g_default_lock;
static int g_default_lock_inited = 0;
static WellLock g_default_init_lock;

static void my_default_lock_ensure(void) {
    if (g_default_lock_inited) return;
    well_lock_acquire(&g_default_init_lock);
    if (!g_default_lock_inited) {
        well_mutex_init(&g_default_lock);
        g_default_lock_inited = 1;
    }
    well_lock_release(&g_default_init_lock);
}

/* ---------- sockets ---------- */
#ifdef _WIN32
static unsigned short my_htons(unsigned short v) { return (unsigned short)((v << 8) | (v >> 8)); }
static void my_socket_init(void) {
    if (g_mym) return;
    g_mym = LoadLibraryA("ws2_32.dll");
    if (!g_mym) well_panic("MySQL: cannot load ws2_32.dll");
    mWSAStartup = (int (*)(int, void *))GetProcAddress(g_mym, "WSAStartup");
    mWSAGetLastError = (int (*)(void))GetProcAddress(g_mym, "WSAGetLastError");
    msocket = (MysqlSocket (*)(int, int, int))GetProcAddress(g_mym, "socket");
    mbind = (int (*)(MysqlSocket, const void *, int))GetProcAddress(g_mym, "bind");
    mconnect = (MysqlSocket (*)(MysqlSocket, const void *, int))GetProcAddress(g_mym, "connect");
    mrecv = (int (*)(MysqlSocket, char *, int, int))GetProcAddress(g_mym, "recv");
    msend = (int (*)(MysqlSocket, const char *, int, int))GetProcAddress(g_mym, "send");
    mselect = (int (*)(int, void *, void *, void *, const void *))GetProcAddress(g_mym, "select");
    msetsockopt = (int (*)(MysqlSocket, int, int, const char *, int))GetProcAddress(g_mym, "setsockopt");
    mgetsockopt = (int (*)(MysqlSocket, int, int, char *, int *))GetProcAddress(g_mym, "getsockopt");
    mioctlsocket = (int (*)(MysqlSocket, long, unsigned long *))GetProcAddress(g_mym, "ioctlsocket");
    mshutdown = (int (*)(MysqlSocket, int))GetProcAddress(g_mym, "shutdown");
    mclosesocket = (int (*)(MysqlSocket))GetProcAddress(g_mym, "closesocket");
    if (!mWSAStartup || !mWSAGetLastError || !msocket || !mconnect || !mrecv ||
        !msend || !mselect || !msetsockopt || !mgetsockopt || !mioctlsocket ||
        !mshutdown || !mclosesocket)
        well_panic("MySQL: ws2_32 exports missing");
    { char wsa[512]; memset(wsa, 0, sizeof(wsa)); if (mWSAStartup(0x0202, wsa) != 0) well_panic("MySQL: WSAStartup failed"); }
}
static MysqlSocket my_socket(void) { return msocket(2, 1, 0); }
static void my_close(MysqlSocket s) { if (s != MYSQL_INVALID_SOCKET) mclosesocket(s); }
static void my_shutdown(MysqlSocket s) { if (s != MYSQL_INVALID_SOCKET) mshutdown(s, 2); }
static int my_recv(MysqlSocket s, char *b, int n) { return mrecv(s, b, n, 0); }
static int my_send(MysqlSocket s, const char *b, int n) { return msend(s, b, n, 0); }
static int my_io_is_timeout(void) {
    int e = mWSAGetLastError();
    return e == MYSQL_WSAETIMEDOUT || e == MYSQL_WSAEWOULDBLOCK;
}
static int my_set_timeouts(MysqlSocket s) {
    unsigned long ms = (unsigned long)MYSQL_IO_TIMEOUT_MS;
    if (msetsockopt(s, MYSQL_SOL_SOCKET, MYSQL_SO_RCVTIMEO, (const char *)&ms, (int)sizeof(ms)) != 0) return -1;
    if (msetsockopt(s, MYSQL_SOL_SOCKET, MYSQL_SO_SNDTIMEO, (const char *)&ms, (int)sizeof(ms)) != 0) return -1;
    return 0;
}
/* 0 = connected, -1 = failure, -2 = timed out. */
static int my_connect_timed(MysqlSocket s, const MysqlAddr *a, int timeout_ms) {
    unsigned long nonblock = 1, blocking = 0;
    MysqlFdSet wr;
    MysqlTimeval tv;
    int r;
    if (mioctlsocket(s, MYSQL_FIONBIO, &nonblock) != 0) return -1;
    r = mconnect(s, a, (int)sizeof(*a));
    if (r == 0) { mioctlsocket(s, MYSQL_FIONBIO, &blocking); return 0; }
    if (mWSAGetLastError() != MYSQL_WSAEWOULDBLOCK) {
        mioctlsocket(s, MYSQL_FIONBIO, &blocking);
        return -1;
    }
    memset(&wr, 0, sizeof(wr));
    wr.count = 1;
    wr.fds[0] = s;
    tv.sec = timeout_ms / 1000;
    tv.usec = (timeout_ms % 1000) * 1000;
    r = mselect(0, NULL, &wr, NULL, &tv);
    mioctlsocket(s, MYSQL_FIONBIO, &blocking);
    if (r == 0) return -2;
    if (r < 0) return -1;
    {
        int soerr = 0, slen = (int)sizeof(soerr);
        if (mgetsockopt(s, MYSQL_SOL_SOCKET, MYSQL_SO_ERROR, (char *)&soerr, &slen) != 0 || soerr != 0)
            return -1;
    }
    return 0;
}
#else
static unsigned short my_htons(unsigned short v) { return htons(v); }
static void my_socket_init(void) { signal(SIGPIPE, SIG_IGN); }
static MysqlSocket my_socket(void) { return socket(AF_INET, SOCK_STREAM, 0); }
static void my_close(MysqlSocket s) { if (s >= 0) close(s); }
static void my_shutdown(MysqlSocket s) { if (s >= 0) shutdown(s, SHUT_RDWR); }
static int my_recv(MysqlSocket s, char *b, int n) { return (int)recv(s, b, (size_t)n, 0); }
static int my_send(MysqlSocket s, const char *b, int n) { return (int)send(s, b, (size_t)n, 0); }
static int my_io_is_timeout(void) {
    return errno == EAGAIN || errno == EWOULDBLOCK || errno == ETIMEDOUT;
}
static int my_set_timeouts(MysqlSocket s) {
    struct timeval tv;
    tv.tv_sec = MYSQL_IO_TIMEOUT_MS / 1000;
    tv.tv_usec = (MYSQL_IO_TIMEOUT_MS % 1000) * 1000;
    if (setsockopt(s, SOL_SOCKET, SO_RCVTIMEO, (const char *)&tv, sizeof(tv)) != 0) return -1;
    if (setsockopt(s, SOL_SOCKET, SO_SNDTIMEO, (const char *)&tv, sizeof(tv)) != 0) return -1;
    return 0;
}
/* 0 = connected, -1 = failure, -2 = timed out. */
static int my_connect_timed(MysqlSocket s, const MysqlAddr *a, int timeout_ms) {
    int flags = fcntl(s, F_GETFL, 0);
    struct pollfd pfd;
    int r;
    if (flags < 0) return -1;
    if (fcntl(s, F_SETFL, flags | O_NONBLOCK) != 0) return -1;
    r = connect(s, (const struct sockaddr *)a, sizeof(*a));
    if (r == 0) { fcntl(s, F_SETFL, flags); return 0; }
    if (errno != EINPROGRESS) { fcntl(s, F_SETFL, flags); return -1; }
    pfd.fd = s;
    pfd.events = POLLOUT;
    pfd.revents = 0;
    r = poll(&pfd, 1, timeout_ms);
    fcntl(s, F_SETFL, flags);
    if (r == 0) return -2;
    if (r < 0) return -1;
    {
        int soerr = 0;
        socklen_t slen = (socklen_t)sizeof(soerr);
        if (getsockopt(s, SOL_SOCKET, SO_ERROR, (char *)&soerr, &slen) != 0 || soerr != 0)
            return -1;
    }
    return 0;
}
#endif

/* Copies a low-level I/O failure into the connection's error slot so the panic
 * message can say "timed out after N ms" instead of a generic "failed". */
static int my_read_all(WellMysqlConn *c, char *b, size_t n) {
    /* Drain anything already buffered first. */
    if (c->read_pos < c->read_end) {
        size_t avail = (size_t)(c->read_end - c->read_pos);
        size_t take = avail < n ? avail : n;
        memcpy(b, c->read_buf + c->read_pos, take);
        c->read_pos += (int)take;
        b += take;
        n -= take;
    }
    while (n) {
        int k;
        if (n >= sizeof(c->read_buf)) {
            /* Large read: straight into the caller's buffer (no copy). */
            k = my_recv(c->fd, b, (int)n);
            if (k == 0) { c->io_err = MY_IO_EOF; return -1; }
            if (k < 0) { c->io_err = my_io_is_timeout() ? MY_IO_TIMEOUT : MY_IO_ERROR; return -1; }
            b += k;
            n -= (size_t)k;
            continue;
        }
        k = my_recv(c->fd, c->read_buf, (int)sizeof(c->read_buf));
        if (k == 0) { c->io_err = MY_IO_EOF; return -1; }
        if (k < 0) { c->io_err = my_io_is_timeout() ? MY_IO_TIMEOUT : MY_IO_ERROR; return -1; }
        c->read_pos = 0;
        c->read_end = k;
        {
            size_t take = (size_t)k < n ? (size_t)k : n;
            memcpy(b, c->read_buf, take);
            c->read_pos = (int)take;
            b += take;
            n -= take;
        }
    }
    return 0;
}

static int my_send_all(WellMysqlConn *c, const char *p, size_t n) {
    while (n) {
        int k = my_send(c->fd, p, (int)n);
        if (k <= 0) { c->io_err = my_io_is_timeout() ? MY_IO_TIMEOUT : MY_IO_ERROR; return -1; }
        p += k; n -= (size_t)k;
    }
    return 0;
}

/* Panics with the concrete I/O outcome (audit M5 wants an unambiguous error). */
static void my_panic_io(WellMysqlConn *c, const char *what) {
    char msg[192];
    if (c->io_err == MY_IO_TIMEOUT)
        snprintf(msg, sizeof(msg), "MySQL: %s timed out after %d ms", what, MYSQL_IO_TIMEOUT_MS);
    else if (c->io_err == MY_IO_EOF)
        snprintf(msg, sizeof(msg), "MySQL: %s failed: connection closed by server", what);
    else
        snprintf(msg, sizeof(msg), "MySQL: %s failed", what);
    well_panic(msg);
}

/* Re-raises a caught panic to the caller's boundary. The panic message was
 * already printed by the original well_panic(), so do not call it again. */
static void my_reraise(jmp_buf *outer) {
    if (outer) longjmp(*outer, 1);
    exit(WELL_PANIC_EXIT_CODE);
}


/* ---------- SHA1 ---------- */
typedef struct { unsigned int h[5]; unsigned long long len; unsigned char buf[64]; size_t n; } Sha1;
static unsigned int rol(unsigned int x, int n) { return (x << n) | (x >> (32 - n)); }
static void sha1_block(Sha1 *s, const unsigned char *p) {
    unsigned int w[80], a, b, c, d, e; int i;
    for (i = 0; i < 16; i++) w[i] = ((unsigned)p[i*4]<<24)|((unsigned)p[i*4+1]<<16)|((unsigned)p[i*4+2]<<8)|p[i*4+3];
    for (i = 16; i < 80; i++) w[i] = rol(w[i-3]^w[i-8]^w[i-14]^w[i-16], 1);
    a=s->h[0]; b=s->h[1]; c=s->h[2]; d=s->h[3]; e=s->h[4];
    for (i = 0; i < 80; i++) {
        unsigned f, k, t;
        if (i < 20) { f=(b&c)|((~b)&d); k=0x5A827999u; }
        else if (i < 40) { f=b^c^d; k=0x6ED9EBA1u; }
        else if (i < 60) { f=(b&c)|(b&d)|(c&d); k=0x8F1BBCDCu; }
        else { f=b^c^d; k=0xCA62C1D6u; }
        t = rol(a,5)+f+e+k+w[i]; e=d; d=c; c=rol(b,30); b=a; a=t;
    }
    s->h[0]+=a; s->h[1]+=b; s->h[2]+=c; s->h[3]+=d; s->h[4]+=e;
}
static void sha1_init(Sha1 *s) { s->h[0]=0x67452301u; s->h[1]=0xEFCDAB89u; s->h[2]=0x98BADCFEu; s->h[3]=0x10325476u; s->h[4]=0xC3D2E1F0u; s->len=0; s->n=0; }
static void sha1_update(Sha1 *s, const void *d, size_t n) {
    const unsigned char *p = (const unsigned char *)d; s->len += (unsigned long long)n*8;
    while (n) {
        size_t take = 64 - s->n; if (take > n) take = n;
        memcpy(s->buf + s->n, p, take); s->n += take; p += take; n -= take;
        if (s->n == 64) { sha1_block(s, s->buf); s->n = 0; }
    }
}
static void sha1_final(Sha1 *s, unsigned char out[20]) {
    unsigned char pad[72]; size_t padn; unsigned long long bits = s->len; int i;
    pad[0] = 0x80; padn = (s->n < 56) ? (56 - s->n) : (120 - s->n);
    memset(pad + 1, 0, padn - 1); sha1_update(s, pad, padn);
    for (i = 0; i < 8; i++) pad[i] = (unsigned char)(bits >> (56 - i*8));
    sha1_update(s, pad, 8);
    for (i = 0; i < 5; i++) {
        out[i*4]=(unsigned char)(s->h[i]>>24); out[i*4+1]=(unsigned char)(s->h[i]>>16);
        out[i*4+2]=(unsigned char)(s->h[i]>>8); out[i*4+3]=(unsigned char)s->h[i];
    }
}
static void my_sha1(const unsigned char *data, size_t n, unsigned char out[20]) {
    Sha1 s; sha1_init(&s); sha1_update(&s, data, n); sha1_final(&s, out);
}

/* ---------- packet io ---------- */
static int my_read_packet(WellMysqlConn *c, unsigned char **out, size_t *outlen) {
    unsigned char h[4]; int len;
    if (my_read_all(c, (char *)h, 4) != 0) return -1;
    len = h[0] | (h[1] << 8) | (h[2] << 16);
    c->seq = (unsigned char)(h[3] + 1);
    *out = (unsigned char *)malloc((size_t)len + 1);
    if (!*out) well_panic("out of memory (MySQL packet)");
    if (len > 0 && my_read_all(c, (char *)*out, (size_t)len) != 0) { free(*out); *out = NULL; return -1; }
    (*out)[len] = '\0'; *outlen = (size_t)len; return 0;
}
static int my_write_packet(WellMysqlConn *c, const unsigned char *p, size_t n) {
    unsigned char h[4];
    h[0]=(unsigned char)n; h[1]=(unsigned char)(n>>8); h[2]=(unsigned char)(n>>16); h[3]=c->seq++;
    if (n <= 512) {
        /* One send for the common small query instead of header+payload. */
        unsigned char buf[4 + 512];
        memcpy(buf, h, 4);
        if (n) memcpy(buf + 4, p, n);
        return my_send_all(c, (const char *)buf, n + 4);
    }
    if (my_send_all(c, (const char *)h, 4) != 0) return -1;
    return my_send_all(c, (const char *)p, n);
}
static int my_read_lenenc(const unsigned char *p, size_t len, size_t *pos, unsigned long long *v) {
    unsigned char c;
    if (*pos >= len) return -1;
    c = p[(*pos)++];
    if (c < 251) { *v = c; return 0; }
    if (c == 251) { *v = 0; return 1; }
    if (c == 252) { if (*pos+2 > len) return -1; *v = p[*pos] | (p[*pos+1]<<8); *pos += 2; return 0; }
    if (c == 253) { if (*pos+3 > len) return -1; *v = p[*pos] | (p[*pos+1]<<8) | ((unsigned long long)p[*pos+2]<<16); *pos += 3; return 0; }
    if (c == 254) { int i; unsigned long long x=0; if (*pos+8 > len) return -1; for (i=0;i<8;i++) x |= ((unsigned long long)p[*pos+i]) << (8*i); *pos += 8; *v=x; return 0; }
    return -1;
}
static char *my_read_lenenc_str(const unsigned char *p, size_t len, size_t *pos,
                                int *is_null, size_t *out_len) {
    unsigned long long n; int r;
    *is_null = 0;
    if (out_len) *out_len = 0;
    r = my_read_lenenc(p, len, pos, &n);
    if (r == 1) { *is_null = 1; return NULL; }
    if (r != 0) return NULL;
    /* Audit M6: "*pos + n" wraps for hostile 8-byte lengths; compare against
     * the bytes that are actually left and cap the copy at 16MB. */
    if (n > (unsigned long long)(len - *pos)) return NULL;
    if (n > 16ULL * 1024 * 1024) return NULL;
    {
        char *s = (char *)malloc((size_t)n + 1);
        if (!s) well_panic("out of memory (MySQL string)");
        memcpy(s, p + *pos, (size_t)n);
        s[n] = '\0';
        *pos += (size_t)n;
        if (out_len) *out_len = (size_t)n;
        return s;
    }
}

/* ---------- auth ---------- */
static void my_native_token(const char *password, const unsigned char *scramble, unsigned char out[20]) {
    unsigned char s1[20], s2[20], mix[20 + 20];
    my_sha1((const unsigned char *)password, strlen(password), s1);
    my_sha1(s1, 20, s2);
    memcpy(mix, scramble, 20); memcpy(mix + 20, s2, 20);
    my_sha1(mix, 40, out);
    { int i; for (i = 0; i < 20; i++) out[i] ^= s1[i]; }
}

static int my_auth(WellMysqlConn *c, const char *user, const char *password, const char *db) {
    unsigned char *pkt = NULL; size_t plen = 0, pos = 0;
    unsigned char scramble[20]; char plugin[64] = "mysql_native_password";
    unsigned int caps;
    unsigned char resp[512]; size_t rn = 0;
    unsigned char token[20];
    c->io_err = MY_IO_OK;
    if (my_read_packet(c, &pkt, &plen) != 0) return -1;
    if (plen < 1 || pkt[0] != 10) { free(pkt); return -1; }
    pos = 1;
    while (pos < plen && pkt[pos]) pos++;
    /* Audit M7: every field read below is bounds-checked. A truncated or
     * hostile handshake used to read past the packet (memcpy from pkt+pos
     * with pos > plen). */
    if (pos >= plen) { free(pkt); return -1; } /* unterminated server version */
    pos++;                                     /* NUL after the version */
    memset(scramble, 0, sizeof(scramble));
    if (plen - pos < 31) { free(pkt); return -1; } /* fixed handshake tail */
    pos += 4;                                  /* thread id */
    memcpy(scramble, pkt + pos, 8); pos += 8;  /* auth-plugin-data part 1 */
    pos++;                                     /* filler */
    caps = (unsigned)pkt[pos] | ((unsigned)pkt[pos + 1] << 8); pos += 2;
    pos++;                                     /* charset */
    pos += 2;                                  /* status flags */
    caps |= ((unsigned)pkt[pos] | ((unsigned)pkt[pos + 1] << 8)) << 16; pos += 2;
    pos++;                                     /* auth plugin data len */
    pos += 10;                                 /* reserved */
    if (caps & 0x8000) {
        if (plen - pos < 12) { free(pkt); return -1; }
        memcpy(scramble + 8, pkt + pos, 12);
        pos += 12;
        if (pos < plen && pkt[pos] == 0) pos++;
    }
    if (caps & 0x80000) {
        size_t k = 0;
        while (pos < plen && pkt[pos] && k < sizeof(plugin) - 1)
            plugin[k++] = (char)pkt[pos++];
        plugin[k] = '\0';
    }
    free(pkt);
    if (strcmp(plugin, "mysql_native_password") != 0) {
        well_panic("MySQL: only mysql_native_password is supported");
    }
    my_native_token(password ? password : "", scramble, token);
    {
        /* Audit M10: resp[] is a fixed 512-byte stack buffer; reject absurd
         * user/database names instead of overflowing it. */
        size_t ulen = strlen(user ? user : "");
        size_t dlen = (db && db[0]) ? strlen(db) : 0;
        size_t need = 4 /* caps */ + 4 /* max packet */ + 1 /* charset */
                    + 23 /* filler */ + ulen + 1 + 1 + 20 + dlen + 1
                    + strlen(plugin) + 1;
        if (need > sizeof(resp))
            well_panic("MySQL: user/database name too long for handshake");
    }
    caps = 0x00000001u | 0x00000004u | 0x00000008u | 0x00000200u | 0x00002000u | 0x00008000u | 0x00020000u | 0x00080000u;
    resp[rn++] = (unsigned char)caps; resp[rn++] = (unsigned char)(caps>>8);
    resp[rn++] = (unsigned char)(caps>>16); resp[rn++] = (unsigned char)(caps>>24);
    resp[rn++] = 0x00; resp[rn++] = 0x00; resp[rn++] = 0x00; resp[rn++] = 0x01; /* max packet 16MB */
    resp[rn++] = 33; /* utf8_general_ci */
    memset(resp + rn, 0, 23); rn += 23; /* handshake response filler */
    { size_t n = strlen(user ? user : ""); memcpy(resp + rn, user ? user : "", n); rn += n; resp[rn++] = 0; }
    resp[rn++] = 20; memcpy(resp + rn, token, 20); rn += 20;
    if (db && db[0]) { size_t n = strlen(db); memcpy(resp + rn, db, n); rn += n; resp[rn++] = 0; }
    else { resp[rn++] = 0; }
    { size_t n = strlen(plugin); memcpy(resp + rn, plugin, n); rn += n; resp[rn++] = 0; }
    c->seq = 1;
    if (my_write_packet(c, resp, rn) != 0) return -1;
    if (my_read_packet(c, &pkt, &plen) != 0) return -1;
    if (pkt[0] == 0xFE) { /* auth switch */
        char newplugin[64] = { 0 }; size_t k = 1; unsigned char newscr[20];
        size_t z = 0;
        while (k < plen && pkt[k] && z < sizeof(newplugin) - 1)
            newplugin[z++] = (char)pkt[k++];
        newplugin[z] = '\0';
        k++; /* NUL after the plugin name */
        /* Audit M7: refuse a truncated scramble instead of using an
         * uninitialised stack buffer. */
        if (plen - k < 20) { free(pkt); return -1; }
        memcpy(newscr, pkt + k, 20);
        if (strcmp(newplugin, "mysql_native_password") != 0) { free(pkt); well_panic("MySQL: unsupported auth switch plugin"); }
        my_native_token(password ? password : "", newscr, token);
        c->seq = 2;
        if (my_write_packet(c, token, 20) != 0) { free(pkt); return -1; }
        free(pkt); pkt = NULL;
        if (my_read_packet(c, &pkt, &plen) != 0) return -1;
    }
    if (pkt[0] == 0xFF) {
        char msg[512]; size_t n = plen > 3 ? plen - 3 : 0; if (n > sizeof(msg)-1) n = sizeof(msg)-1;
        memcpy(msg, pkt + 3, n); msg[n] = '\0'; free(pkt); well_panic(msg); return -1;
    }
    if (pkt[0] != 0x00) { free(pkt); well_panic("MySQL: unexpected auth response"); return -1; }
    free(pkt); return 0;
}

/* ---------- connect/exec/query ---------- */
static void my_connect_conn(WellMysqlConn *c, const char *host, int port,
                            const char *user, const char *password, const char *db) {
    MysqlAddr addr;
    int cr;
    char msg[192];
    my_socket_init();
    if (c->connected) { my_close(c->fd); c->connected = 0; }
    c->read_pos = 0;
    c->read_end = 0;
    memset(&addr, 0, sizeof(addr));
    addr.family = 2; addr.port = my_htons((unsigned short)port);
    { unsigned int ip = 0, a, b, cc, d;
      if (sscanf(host, "%u.%u.%u.%u", &a, &b, &cc, &d) == 4)
          ip = a | (b << 8) | (cc << 16) | (d << 24);
      addr.addr = ip; }
    c->io_err = MY_IO_OK;
    c->fd = my_socket();
    if (c->fd == MYSQL_INVALID_SOCKET) well_panic("MySQL: socket failed");
    if (my_set_timeouts(c->fd) != 0) {
        my_close(c->fd);
        c->fd = MYSQL_INVALID_SOCKET;
        well_panic("MySQL: cannot set socket timeouts");
    }
    cr = my_connect_timed(c->fd, &addr, MYSQL_CONNECT_TIMEOUT_MS);
    if (cr != 0) {
        my_close(c->fd);
        c->fd = MYSQL_INVALID_SOCKET;
        if (cr == -2)
            snprintf(msg, sizeof(msg), "MySQL: connect to %s:%d timed out after %d ms",
                     host, port, MYSQL_CONNECT_TIMEOUT_MS);
        else
            snprintf(msg, sizeof(msg), "MySQL: connect to %s:%d failed", host, port);
        well_panic(msg);
    }
    c->seq = 0;
    if (my_auth(c, user, password, db) != 0) {
        if (c->io_err != MY_IO_OK) my_panic_io(c, "auth read");
        well_panic("MySQL: auth failed");
    }
    c->connected = 1;
}

WellMysqlConn *well_mysql_conn_new(const char *host, int port, const char *user,
                                   const char *password, const char *db) {
    WellMysqlConn *c = (WellMysqlConn *)calloc(1, sizeof(WellMysqlConn));
    if (!c) well_panic("out of memory (MySQL conn)");
    my_connect_conn(c, host, port, user, password, db);
    return c;
}

void well_mysql_connect(const char *host, int port, const char *user,
                        const char *password, const char *db) {
    jmp_buf jb;
    jmp_buf *old;
    my_default_lock_ensure();
    well_mutex_lock(&g_default_lock);
    old = well_panic_push(&jb);
    if (setjmp(jb) == 0) {
        my_connect_conn(&g_default, host, port, user, password, db);
        well_panic_pop(old);
        well_mutex_unlock(&g_default_lock);
        return;
    }
    well_panic_pop(old);
    well_mutex_unlock(&g_default_lock);
    my_reraise(old);
}

/* Copies the server error message out of an ERR packet (0xFF) into msg. */
static void my_err_message(const unsigned char *pkt, size_t plen, char *msg, size_t cap) {
    size_t n = plen > 3 ? plen - 3 : 0;
    if (n > cap - 1) n = cap - 1;
    memcpy(msg, pkt + 3, n);
    msg[n] = '\0';
}

/* exec() only expects OK/ERR. A column-count packet means the statement
 * returned a result set; the column definitions, the EOF after them and every
 * row are still queued on the socket. Consume them (so the next command starts
 * on a packet boundary) before reporting the error. Returns 0 when the result
 * set was fully drained, -1 when the stream can no longer be trusted (the
 * caller must close the connection). */
static int my_discard_resultset(WellMysqlConn *c, const unsigned char *first, size_t plen) {
    size_t pos = 0;
    unsigned long long cols = 0, i;
    unsigned char *pkt = NULL;
    size_t n = 0;
    if (my_read_lenenc(first, plen, &pos, &cols) != 0 || cols > 4096) return -1;
    for (i = 0; i < cols; i++) {
        if (my_read_packet(c, &pkt, &n) != 0) return -1;
        free(pkt);
        pkt = NULL;
    }
    /* Terminator of the column-definition block: EOF (0xFE) or OK (0x00). */
    if (my_read_packet(c, &pkt, &n) != 0) return -1;
    if (!(n > 0 && (pkt[0] == 0xFE || pkt[0] == 0x00))) { free(pkt); return -1; }
    free(pkt);
    pkt = NULL;
    for (;;) {
        if (my_read_packet(c, &pkt, &n) != 0) return -1;
        if (n > 0 && pkt[0] == 0xFF) { free(pkt); return 0; } /* ERR ends the command */
        if (n > 0 && pkt[0] == 0xFE && n < 9) { free(pkt); return 0; }
        free(pkt);
        pkt = NULL;
    }
}

int well_mysql_conn_exec(WellMysqlConn *c, const char *sql) {
    unsigned char *pkt = NULL; size_t plen = 0; long long affected = 0;
    size_t n = strlen(sql);
    unsigned char qstack[256];
    unsigned char *q = (n + 1 <= sizeof(qstack)) ? qstack
                                                 : (unsigned char *)malloc(n + 1);
    if (!q) well_panic("out of memory (MySQL query)");
    if (!c->connected) { if (q != qstack) free(q); well_panic("MySQL: not connected"); }
    c->io_err = MY_IO_OK;
    q[0] = 0x03; memcpy(q + 1, sql, n); c->seq = 0;
    if (my_write_packet(c, q, n + 1) != 0) { if (q != qstack) free(q); my_panic_io(c, "send"); }
    if (q != qstack) free(q);
    if (my_read_packet(c, &pkt, &plen) != 0) my_panic_io(c, "read");
    if (plen > 0 && pkt[0] == 0xFF) {
        char msg[512];
        my_err_message(pkt, plen, msg, sizeof(msg));
        free(pkt);
        well_panic(msg);
    }
    if (plen > 0 && pkt[0] == 0x00) {
        size_t pos = 1; unsigned long long a;
        if (my_read_lenenc(pkt, plen, &pos, &a) == 0) affected = (long long)a;
        free(pkt);
        /* Audit M13: the public API returns int; clamp instead of wrapping. */
        if (affected > 2147483647LL) return 2147483647;
        return (int)affected;
    }
    /* Unexpected packet (column count): drain the whole result set, then fail. */
    if (my_discard_resultset(c, pkt, plen) != 0) {
        free(pkt);
        well_mysql_conn_close(c); /* packet boundary lost: never reuse this conn */
        if (c->io_err != MY_IO_OK) my_panic_io(c, "exec: result-set drain read");
        well_panic("MySQL: exec: statement returned a result set (connection closed)");
    }
    free(pkt);
    well_panic("MySQL: exec: statement returned a result set (use Mysql.queryJson)");
    return 0;
}

/* Grow the JSON result buffer so that at least ``need`` bytes are usable
 * (including the terminating NUL). Growth happens before writing, never
 * after: a field longer than the current capacity used to write past the
 * end of the 1KB initial buffer. */
static char *mysql_json_reserve(char *buf, size_t *cap, size_t need) {
    size_t nc = *cap;
    char *nb;
    if (need <= nc) return buf;
    while (nc < need) {
        if (nc > ((size_t)-1) / 2) well_panic("out of memory (MySQL json)");
        nc *= 2;
    }
    nb = (char *)realloc(buf, nc);
    if (!nb) well_panic("out of memory (MySQL json)");
    *cap = nc;
    return nb;
}

/* Audit M8: escapes one string segment into the JSON buffer. Control bytes
 * become \u00XX (the old code emitted them raw, which is invalid JSON) and the
 * caller passes the lenenc length, so embedded NUL bytes are preserved as
 * \u0000 instead of truncating the value. */
static char *mysql_json_escape(char *buf, size_t *cap, size_t *len,
                               const char *s, size_t n) {
    static const char hexd[] = "0123456789abcdef";
    size_t j;
    for (j = 0; j < n; j++) {
        unsigned char ch = (unsigned char)s[j];
        buf = mysql_json_reserve(buf, cap, *len + 8);
        if (ch == '"' || ch == '\\') {
            buf[(*len)++] = '\\';
            buf[(*len)++] = (char)ch;
        } else if (ch == '\n') {
            buf[(*len)++] = '\\'; buf[(*len)++] = 'n';
        } else if (ch == '\r') {
            buf[(*len)++] = '\\'; buf[(*len)++] = 'r';
        } else if (ch == '\t') {
            buf[(*len)++] = '\\'; buf[(*len)++] = 't';
        } else if (ch < 0x20) {
            buf[(*len)++] = '\\';
            buf[(*len)++] = 'u';
            buf[(*len)++] = '0';
            buf[(*len)++] = '0';
            buf[(*len)++] = hexd[(ch >> 4) & 0x0F];
            buf[(*len)++] = hexd[ch & 0x0F];
        } else {
            buf[(*len)++] = (char)ch;
        }
    }
    return buf;
}

WellString *well_mysql_conn_query_json(WellMysqlConn *c, const char *sql) {
    unsigned char *pkt = NULL; size_t plen = 0, pos;
    unsigned long long cols = 0;
    char **names = NULL;
    WellString *out;
    size_t cap = 1024, len = 0; char *buf = (char *)malloc(cap);
    unsigned long long col;
    if (!buf) well_panic("out of memory (MySQL json)");
    if (!c->connected) well_panic("MySQL: not connected");
    c->io_err = MY_IO_OK;
    { size_t n = strlen(sql); unsigned char qstack[256];
      unsigned char *q = (n + 1 <= sizeof(qstack)) ? qstack
                                                   : (unsigned char *)malloc(n + 1);
      if (!q) { free(buf); well_panic("out of memory (MySQL query)"); }
      q[0]=0x03; memcpy(q+1,sql,n); c->seq=0;
      if (my_write_packet(c, q, n + 1) != 0) { if (q != qstack) free(q); my_panic_io(c, "send"); }
      if (q != qstack) free(q); }
    if (my_read_packet(c, &pkt, &plen) != 0) my_panic_io(c, "read");
    if (plen > 0 && pkt[0] == 0xFF) { char msg[512]; size_t n=plen>3?plen-3:0; if(n>sizeof(msg)-1)n=sizeof(msg)-1; memcpy(msg,pkt+3,n); msg[n]='\0'; free(pkt); well_panic(msg); }
    pos = 0;
    if (my_read_lenenc(pkt, plen, &pos, &cols) != 0) { free(pkt); well_panic("MySQL: bad column count"); }
    free(pkt);
    /* Audit M9: the column count comes from the server. Bound it and close the
     * connection before panicking, otherwise the unread packets leave the
     * stream desynchronized for any later use of this connection. */
    if (cols > 4096) {
        well_mysql_conn_close(c);
        well_panic("MySQL: too many columns (connection closed)");
    }
    names = (char **)calloc((size_t)cols, sizeof(char *));
    if (!names) {
        well_mysql_conn_close(c);
        well_panic("out of memory (MySQL columns) (connection closed)");
    }
    for (col = 0; col < cols; col++) {
        size_t pp = 0, k;
        int isnull = 0;
        char *name;
        if (my_read_packet(c, &pkt, &plen) != 0) my_panic_io(c, "read column definition");
        for (k = 0; k < 4; k++) {
            unsigned long long x = 0;
            /* Audit M13: check the lenenc result and bounds before advancing. */
            if (my_read_lenenc(pkt, plen, &pp, &x) != 0 ||
                x > (unsigned long long)(plen - pp)) {
                free(pkt);
                well_mysql_conn_close(c);
                well_panic("MySQL: bad column definition (connection closed)");
            }
            pp += (size_t)x;
        }
        name = my_read_lenenc_str(pkt, plen, &pp, &isnull, NULL);
        if (!name && !isnull) { /* audit M13: don't silently accept garbage */
            free(pkt);
            well_mysql_conn_close(c);
            well_panic("MySQL: bad column name (connection closed)");
        }
        names[col] = name;
        free(pkt);
    }
    if (my_read_packet(c, &pkt, &plen) != 0) my_panic_io(c, "read column terminator");
    free(pkt);
    buf = mysql_json_reserve(buf, &cap, len + 2);
    buf[len++] = '['; buf[len] = '\0';
    {
        int first = 1;
        for (;;) {
            size_t pp = 0;
            if (my_read_packet(c, &pkt, &plen) != 0) my_panic_io(c, "read row");
            if (plen > 0 && pkt[0] == 0xFE && plen < 9) { free(pkt); break; }
            if (!first) {
                buf = mysql_json_reserve(buf, &cap, len + 2);
                buf[len++] = ',';
            }
            first = 0;
            buf = mysql_json_reserve(buf, &cap, len + 2);
            buf[len++] = '{';
            for (col = 0; col < cols; col++) {
                int isnull = 0;
                size_t vlen = 0;
                char *val = my_read_lenenc_str(pkt, plen, &pp, &isnull, &vlen);
                if (!val && !isnull) {
                    free(pkt);
                    well_mysql_conn_close(c);
                    well_panic("MySQL: bad row value (connection closed)");
                }
                if (col) {
                    buf = mysql_json_reserve(buf, &cap, len + 2);
                    buf[len++] = ',';
                }
                buf = mysql_json_reserve(buf, &cap, len + 2);
                buf[len++] = '"';
                buf = mysql_json_escape(buf, &cap, &len,
                                        names[col] ? names[col] : "",
                                        names[col] ? strlen(names[col]) : 0);
                buf = mysql_json_reserve(buf, &cap, len + 3);
                buf[len++] = '"'; buf[len++] = ':';
                if (isnull) {
                    buf = mysql_json_reserve(buf, &cap, len + 5);
                    memcpy(buf + len, "null", 4); len += 4;
                } else {
                    buf = mysql_json_reserve(buf, &cap, len + 2);
                    buf[len++] = '"';
                    /* Audit M8: length-based copy keeps embedded NULs. */
                    buf = mysql_json_escape(buf, &cap, &len, val, vlen);
                    buf = mysql_json_reserve(buf, &cap, len + 2);
                    buf[len++] = '"';
                }
                free(val);
            }
            buf = mysql_json_reserve(buf, &cap, len + 2);
            buf[len++] = '}'; buf[len] = '\0';
            free(pkt);
        }
    }
    buf = mysql_json_reserve(buf, &cap, len + 2);
    buf[len++] = ']'; buf[len] = '\0';
    out = well_string_new_n(buf, len);
    free(buf);
    for (col = 0; col < cols; col++) free(names[col]);
    free(names);
    return out;
}

int well_mysql_exec(const char *sql) {
    int r;
    jmp_buf jb;
    jmp_buf *old;
    my_default_lock_ensure();
    well_mutex_lock(&g_default_lock);
    old = well_panic_push(&jb);
    if (setjmp(jb) == 0) {
        r = well_mysql_conn_exec(&g_default, sql);
        well_panic_pop(old);
        well_mutex_unlock(&g_default_lock);
        return r;
    }
    /* Release the default-connection lock before re-raising (audit M11). */
    well_panic_pop(old);
    well_mutex_unlock(&g_default_lock);
    my_reraise(old);
    return 0; /* not reached */
}

WellString *well_mysql_query_json(const char *sql) {
    WellString *r;
    jmp_buf jb;
    jmp_buf *old;
    my_default_lock_ensure();
    well_mutex_lock(&g_default_lock);
    old = well_panic_push(&jb);
    if (setjmp(jb) == 0) {
        r = well_mysql_conn_query_json(&g_default, sql);
        well_panic_pop(old);
        well_mutex_unlock(&g_default_lock);
        return r;
    }
    well_panic_pop(old);
    well_mutex_unlock(&g_default_lock);
    my_reraise(old);
    return NULL; /* not reached */
}

void well_mysql_conn_close(WellMysqlConn *c) {
    if (!c) return;
    if (c->connected) { my_close(c->fd); c->connected = 0; }
    c->read_pos = 0;
    c->read_end = 0;
}

void well_mysql_close(void) {
    my_default_lock_ensure();
    well_mutex_lock(&g_default_lock);
    well_mysql_conn_close(&g_default);
    well_mutex_unlock(&g_default_lock);
}

/* Audit M12: cheap liveness check for a pooled connection. Idle connections can
 * be killed by a server restart or by wait_timeout; without COM_PING the next
 * statement on the stale socket would panic. */
static int my_ping(WellMysqlConn *c) {
    unsigned char cmd[1];
    unsigned char *pkt = NULL;
    size_t plen = 0;
    int ok;
    if (!c->connected) return -1;
    c->io_err = MY_IO_OK;
    c->seq = 0;
    cmd[0] = 0x0E; /* COM_PING */
    if (my_write_packet(c, cmd, 1) != 0) return -1;
    if (my_read_packet(c, &pkt, &plen) != 0) return -1;
    ok = (plen > 0 && pkt[0] == 0x00);
    free(pkt);
    return ok ? 0 : -1;
}

/* ---------- connection pool ---------- */
/* closePool() must never wait forever: a connection that was never released
 * (buggy/aborted handler) would otherwise hang the caller permanently. */
#define MYSQL_POOL_CLOSE_TIMEOUT_MS 5000

struct WellMysqlPool {
    WellMutex lock;
    char *host, *user, *password, *db;
    int port;
    int min_conn, max_conn;
    WellMysqlConn **idle;
    int idle_n, idle_cap;
    int total, active;
    int closing;
    WellMysqlConn *all; /* every live connection (idle + checked out) */
};

/* Caller holds pool->lock. */
static void my_pool_track_locked(WellMysqlPool *p, WellMysqlConn *c) {
    c->next_all = p->all;
    p->all = c;
}

/* Caller holds pool->lock. */
static void my_pool_untrack_locked(WellMysqlPool *p, WellMysqlConn *c) {
    WellMysqlConn **pp = &p->all;
    while (*pp) {
        if (*pp == c) {
            *pp = c->next_all;
            c->next_all = NULL;
            return;
        }
        pp = &(*pp)->next_all;
    }
}

static char *my_strdup2(const char *s) {
    size_t n = strlen(s ? s : "");
    char *p = (char *)malloc(n + 1);
    if (!p) well_panic("out of memory (MySQL pool)");
    memcpy(p, s ? s : "", n + 1);
    return p;
}

WellMysqlPool *well_mysql_pool_new(const char *host, int port,
                                   const char *user, const char *password,
                                   const char *db, int min_conn, int max_conn) {
    WellMysqlPool *p = (WellMysqlPool *)calloc(1, sizeof(WellMysqlPool));
    int i;
    if (!p) well_panic("out of memory (MySQL pool)");
    if (max_conn < 1) max_conn = 1;
    if (min_conn < 0) min_conn = 0;
    if (min_conn > max_conn) min_conn = max_conn;
    well_mutex_init(&p->lock);
    p->host = my_strdup2(host);
    p->user = my_strdup2(user);
    p->password = my_strdup2(password);
    p->db = my_strdup2(db);
    p->port = port;
    p->min_conn = min_conn; p->max_conn = max_conn;
    p->idle_cap = max_conn;
    p->idle = (WellMysqlConn **)calloc((size_t)max_conn, sizeof(WellMysqlConn *));
    if (!p->idle) well_panic("out of memory (MySQL pool idle)");
    for (i = 0; i < min_conn; i++) {
        WellMysqlConn *c = well_mysql_conn_new(host, port, user, password, db);
        p->idle[p->idle_n++] = c;
        p->total++;
        my_pool_track_locked(p, c);
    }
    return p;
}

WellMysqlConn *well_mysql_pool_acquire(WellMysqlPool *pool, int timeout_ms) {
    long long deadline = timeout_ms < 0 ? -1 : well_mono_ms() + timeout_ms;
    if (!pool) return NULL;
    my_ping_cfg_init();
    for (;;) {
        WellMysqlConn *c = NULL;
        well_mutex_lock(&pool->lock);
        if (pool->closing) { well_mutex_unlock(&pool->lock); return NULL; }
        if (pool->idle_n > 0) {
            long long now;
            int need_ping;
            c = pool->idle[--pool->idle_n];
            pool->active++;
            c->checked_out = 1; /* audit M12 */
            well_mutex_unlock(&pool->lock);
            now = well_mono_ms();
            need_ping = (g_ping_idle_ms >= 0) &&
                        (g_ping_idle_ms == 0 || now - c->last_used_ms >= g_ping_idle_ms);
            if (!c->connected || (need_ping && my_ping(c) != 0)) {
                /* Stale idle connection: drop it and continue with a fresh one
                 * instead of handing a dead socket to the caller. */
                well_mysql_conn_close(c);
                well_mutex_lock(&pool->lock);
                pool->total--;
                pool->active--;
                my_pool_untrack_locked(pool, c);
                well_mutex_unlock(&pool->lock);
                free(c);
                if (deadline >= 0 && well_mono_ms() >= deadline) return NULL;
                continue;
            }
            c->last_used_ms = now;
            return c;
        }
        if (pool->total < pool->max_conn) {
            /* Reserve the slot, connect outside the lock, then publish it. A
             * failed connect must undo the reservation before panicking. */
            WellMysqlConn *nc = NULL;
            jmp_buf jb;
            jmp_buf *old;
            pool->total++;
            pool->active++;
            well_mutex_unlock(&pool->lock);
            old = well_panic_push(&jb);
            if (setjmp(jb) == 0) {
                nc = well_mysql_conn_new(pool->host, pool->port, pool->user,
                                         pool->password, pool->db);
                well_panic_pop(old);
            } else {
                well_panic_pop(old);
                well_mutex_lock(&pool->lock);
                pool->total--;
                pool->active--;
                well_mutex_unlock(&pool->lock);
                my_reraise(old);
            }
            well_mutex_lock(&pool->lock);
            if (pool->closing) { /* closePool() raced with the connect */
                pool->total--;
                pool->active--;
                well_mutex_unlock(&pool->lock);
                well_mysql_conn_close(nc);
                free(nc);
                return NULL;
            }
            my_pool_track_locked(pool, nc);
            nc->checked_out = 1; /* audit M12 */
            nc->last_used_ms = well_mono_ms();
            well_mutex_unlock(&pool->lock);
            return nc;
        }
        well_mutex_unlock(&pool->lock);
        if (deadline >= 0 && well_mono_ms() >= deadline) return NULL;
        well_sleep_ms(5);
    }
}

void well_mysql_pool_release(WellMysqlPool *pool, WellMysqlConn *conn) {
    if (!pool || !conn) return;
    well_mutex_lock(&pool->lock);
    /* Audit M12: a double release used to push the same pointer into the idle
     * array twice (overflow / double free) and corrupt the counters. */
    if (!conn->checked_out) {
        well_mutex_unlock(&pool->lock);
        fprintf(stderr,
                "well warning: MySQL pool: release of a connection that is not checked out ignored\n");
        return;
    }
    conn->checked_out = 0;
    conn->last_used_ms = well_mono_ms();
    if (pool->active > 0) pool->active--;
    if (pool->closing || !conn->connected || pool->idle_n >= pool->idle_cap) {
        pool->total--;
        my_pool_untrack_locked(pool, conn);
        well_mutex_unlock(&pool->lock);
        well_mysql_conn_close(conn);
        free(conn);
        return;
    }
    pool->idle[pool->idle_n++] = conn;
    well_mutex_unlock(&pool->lock);
}

/* Runs one pooled statement. Panic uses longjmp, which would otherwise skip
 * the release and permanently leak the checked-out connection (audit M4), so
 * the call is wrapped in a local panic boundary: release, then re-raise. */
int well_mysql_pool_exec(WellMysqlPool *pool, const char *sql) {
    WellMysqlConn *c = well_mysql_pool_acquire(pool, 3000);
    int r;
    jmp_buf jb;
    jmp_buf *old;
    if (!c) well_panic("MySQL pool: acquire timeout");
    old = well_panic_push(&jb);
    if (setjmp(jb) == 0) {
        r = well_mysql_conn_exec(c, sql);
        well_panic_pop(old);
        well_mysql_pool_release(pool, c);
        return r;
    }
    /* The statement panicked: the connection may be mid-packet, so discard it
     * instead of handing a desynchronized socket to the next borrower. */
    well_panic_pop(old);
    well_mysql_conn_close(c);
    well_mysql_pool_release(pool, c);
    my_reraise(old);
    return 0; /* not reached */
}

WellString *well_mysql_pool_query_json(WellMysqlPool *pool, const char *sql) {
    WellMysqlConn *c = well_mysql_pool_acquire(pool, 3000);
    WellString *r;
    jmp_buf jb;
    jmp_buf *old;
    if (!c) well_panic("MySQL pool: acquire timeout");
    old = well_panic_push(&jb);
    if (setjmp(jb) == 0) {
        r = well_mysql_conn_query_json(c, sql);
        well_panic_pop(old);
        well_mysql_pool_release(pool, c);
        return r;
    }
    well_panic_pop(old);
    well_mysql_conn_close(c);
    well_mysql_pool_release(pool, c);
    my_reraise(old);
    return NULL; /* not reached */
}

void well_mysql_pool_close(WellMysqlPool *pool) {
    long long deadline;
    if (!pool) return;
    well_mutex_lock(&pool->lock);
    pool->closing = 1;
    while (pool->idle_n > 0) {
        WellMysqlConn *c = pool->idle[--pool->idle_n];
        my_pool_untrack_locked(pool, c);
        pool->total--;
        well_mysql_conn_close(c);
        free(c);
    }
    /* Unblock checked-out connections that are stuck in send()/recv(): their
     * owners then fail, release the connection and let the drain finish. */
    {
        WellMysqlConn *c;
        for (c = pool->all; c; c = c->next_all) my_shutdown(c->fd);
    }
    well_mutex_unlock(&pool->lock);
    deadline = well_mono_ms() + MYSQL_POOL_CLOSE_TIMEOUT_MS;
    for (;;) {
        int active;
        well_mutex_lock(&pool->lock);
        active = pool->active;
        well_mutex_unlock(&pool->lock);
        if (active <= 0) break;
        if (well_mono_ms() >= deadline) {
            /* Freeing state that a checked-out connection still dereferences
             * would be a use-after-free; leak it instead (bounded wait, no
             * permanent hang). */
            fprintf(stderr,
                    "well warning: MySQL pool: close timed out with %d connection(s) still checked out; pool state leaked\n",
                    active);
            return;
        }
        well_sleep_ms(5);
    }
    free(pool->idle);
    free(pool->host); free(pool->user); free(pool->password); free(pool->db);
    well_mutex_destroy(&pool->lock);
    free(pool);
}

static WellMysqlPool *g_lang_pool = NULL;

void well_mysql_pool_configure(const char *host, int port, const char *user,
                               const char *password, const char *db,
                               int min_conn, int max_conn) {
    if (g_lang_pool) { well_mysql_pool_close(g_lang_pool); g_lang_pool = NULL; }
    g_lang_pool = well_mysql_pool_new(host, port, user, password, db, min_conn, max_conn);
}

int well_mysql_pool_exec_global(const char *sql) {
    if (!g_lang_pool) well_panic("MySQL pool: not configured");
    return well_mysql_pool_exec(g_lang_pool, sql);
}

WellString *well_mysql_pool_query_json_global(const char *sql) {
    if (!g_lang_pool) well_panic("MySQL pool: not configured");
    return well_mysql_pool_query_json(g_lang_pool, sql);
}

void well_mysql_pool_close_global(void) {
    if (g_lang_pool) { well_mysql_pool_close(g_lang_pool); g_lang_pool = NULL; }
}

/* Audit M11: initialize the default-connection lock before any handler can
 * run (module init is called from the generated main). */
static int well_mysql_module_init(void) {
    my_default_lock_ensure();
    return 0;
}

static void well_mysql_module_shutdown(void) { well_mysql_pool_close_global(); well_mysql_close(); }
WellModule well_mysql_module = { "mysql", well_mysql_module_init, NULL, NULL,
                                 well_mysql_module_shutdown };

