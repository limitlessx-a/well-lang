#include "wellrt.h"
#include "well_mysql.h"
#include "well_internal.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#ifdef _WIN32
#include <windows.h>
typedef unsigned long long MysqlSocket;
typedef struct { unsigned short family, port; unsigned long addr; char zero[8]; } MysqlAddr;
#define MYSQL_INVALID_SOCKET (~0ULL)
static HMODULE g_mym;
static int (*mWSAStartup)(int, void *);
static MysqlSocket (*msocket)(int, int, int);
static int (*mbind)(MysqlSocket, const void *, int);
static MysqlSocket (*mconnect)(MysqlSocket, const void *, int);
static int (*mrecv)(MysqlSocket, char *, int, int);
static int (*msend)(MysqlSocket, const char *, int, int);
static int (*mclosesocket)(MysqlSocket);
#else
#include <sys/socket.h>
#include <netinet/in.h>
#include <unistd.h>
#include <signal.h>
typedef int MysqlSocket;
typedef struct { unsigned short family, port; unsigned long addr; char zero[8]; } MysqlAddr;
#define MYSQL_INVALID_SOCKET (-1)
#endif

typedef struct WellMysqlConn {
    MysqlSocket fd;
    unsigned char seq;
    int connected;
} WellMysqlConn;

static WellMysqlConn g_default;

/* ---------- sockets ---------- */
#ifdef _WIN32
static unsigned short my_htons(unsigned short v) { return (unsigned short)((v << 8) | (v >> 8)); }
static void my_socket_init(void) {
    if (g_mym) return;
    g_mym = LoadLibraryA("ws2_32.dll");
    if (!g_mym) well_panic("MySQL: cannot load ws2_32.dll");
    mWSAStartup = (int (*)(int, void *))GetProcAddress(g_mym, "WSAStartup");
    msocket = (MysqlSocket (*)(int, int, int))GetProcAddress(g_mym, "socket");
    mbind = (int (*)(MysqlSocket, const void *, int))GetProcAddress(g_mym, "bind");
    mconnect = (MysqlSocket (*)(MysqlSocket, const void *, int))GetProcAddress(g_mym, "connect");
    mrecv = (int (*)(MysqlSocket, char *, int, int))GetProcAddress(g_mym, "recv");
    msend = (int (*)(MysqlSocket, const char *, int, int))GetProcAddress(g_mym, "send");
    mclosesocket = (int (*)(MysqlSocket))GetProcAddress(g_mym, "closesocket");
    if (!mWSAStartup || !msocket || !mconnect || !mrecv || !msend || !mclosesocket)
        well_panic("MySQL: ws2_32 exports missing");
    { char wsa[512]; memset(wsa, 0, sizeof(wsa)); if (mWSAStartup(0x0202, wsa) != 0) well_panic("MySQL: WSAStartup failed"); }
}
static MysqlSocket my_socket(void) { return msocket(2, 1, 0); }
static void my_close(MysqlSocket s) { if (s != MYSQL_INVALID_SOCKET) mclosesocket(s); }
static int my_recv(MysqlSocket s, char *b, int n) { return mrecv(s, b, n, 0); }
static int my_send(MysqlSocket s, const char *b, int n) { return msend(s, b, n, 0); }
#else
static unsigned short my_htons(unsigned short v) { return htons(v); }
static void my_socket_init(void) { signal(SIGPIPE, SIG_IGN); }
static MysqlSocket my_socket(void) { return socket(AF_INET, SOCK_STREAM, 0); }
static void my_close(MysqlSocket s) { if (s >= 0) close(s); }
static int my_recv(MysqlSocket s, char *b, int n) { return (int)recv(s, b, (size_t)n, 0); }
static int my_send(MysqlSocket s, const char *b, int n) { return (int)send(s, b, (size_t)n, 0); }
#endif

static int my_send_all(MysqlSocket s, const char *p, size_t n) {
    while (n) { int k = my_send(s, p, (int)n); if (k <= 0) return -1; p += k; n -= (size_t)k; }
    return 0;
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
    if (my_recv(c->fd, (char *)h, 4) != 4) return -1;
    len = h[0] | (h[1] << 8) | (h[2] << 16);
    c->seq = (unsigned char)(h[3] + 1);
    *out = (unsigned char *)malloc((size_t)len + 1);
    if (!*out) well_panic("out of memory (MySQL packet)");
    if (len > 0 && my_recv(c->fd, (char *)*out, len) != len) { free(*out); *out = NULL; return -1; }
    (*out)[len] = '\0'; *outlen = (size_t)len; return 0;
}
static int my_write_packet(WellMysqlConn *c, const unsigned char *p, size_t n) {
    unsigned char h[4];
    h[0]=(unsigned char)n; h[1]=(unsigned char)(n>>8); h[2]=(unsigned char)(n>>16); h[3]=c->seq++;
    if (my_send_all(c->fd, (const char *)h, 4) != 0) return -1;
    return my_send_all(c->fd, (const char *)p, n);
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
static char *my_read_lenenc_str(const unsigned char *p, size_t len, size_t *pos, int *is_null) {
    unsigned long long n; int r;
    *is_null = 0;
    r = my_read_lenenc(p, len, pos, &n);
    if (r == 1) { *is_null = 1; return NULL; }
    if (r != 0 || *pos + n > len) return NULL;
    { char *s = (char *)malloc((size_t)n + 1); if (!s) well_panic("out of memory (MySQL string)"); memcpy(s, p + *pos, (size_t)n); s[n] = '\0'; *pos += (size_t)n; return s; }
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
    if (my_read_packet(c, &pkt, &plen) != 0) return -1;
    if (plen < 1 || pkt[0] != 10) { free(pkt); return -1; }
    pos = 1;
    while (pos < plen && pkt[pos]) pos++;
    pos++;
    pos += 4;
    memcpy(scramble, pkt + pos, 8); pos += 8; pos++;
    caps = pkt[pos] | (pkt[pos+1] << 8); pos += 2;
    pos++;
    pos += 2;
    caps |= (unsigned)(pkt[pos] | (pkt[pos+1] << 8)) << 16; pos += 2;
    pos++;
    pos += 10;
    if (caps & 0x8000) { memcpy(scramble + 8, pkt + pos, 12); pos += 12; if (pos < plen && pkt[pos] == 0) pos++; }
    if (caps & 0x80000) {
        size_t k = 0; while (pos < plen && pkt[pos] && k < sizeof(plugin)-1) plugin[k++] = (char)pkt[pos++]; plugin[k] = '\0';
    }
    free(pkt);
    if (strcmp(plugin, "mysql_native_password") != 0) {
        well_panic("MySQL: only mysql_native_password is supported");
    }
    my_native_token(password ? password : "", scramble, token);
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
        char newplugin[64]; size_t k = 1; unsigned char newscr[20];
        while (k < plen && pkt[k]) { k++; } /* plugin name at pkt+1 */ 
        k = 1; { size_t z = 0; while (k < plen && pkt[k] && z < sizeof(newplugin)-1) newplugin[z++] = (char)pkt[k++]; newplugin[z] = '\0'; }
        k++; if (k + 20 <= plen) memcpy(newscr, pkt + k, 20);
        if (strcmp(newplugin, "mysql_native_password") != 0) { free(pkt); well_panic("MySQL: unsupported auth switch plugin"); }
        my_native_token(password ? password : "", newscr, token);
        c->seq = 2;
        my_write_packet(c, token, 20);
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
    my_socket_init();
    if (c->connected) { my_close(c->fd); c->connected = 0; }
    memset(&addr, 0, sizeof(addr));
    addr.family = 2; addr.port = my_htons((unsigned short)port);
    { unsigned long ip = 0; unsigned int a,b,cc,d;
      if (sscanf(host, "%u.%u.%u.%u", &a,&b,&cc,&d) == 4)
          ip = (unsigned long)(a | (b<<8) | (cc<<16) | (d<<24));
      addr.addr = ip; }
    c->fd = my_socket();
    if (c->fd == MYSQL_INVALID_SOCKET) well_panic("MySQL: socket failed");
#ifdef _WIN32
    if (mconnect(c->fd, &addr, (int)sizeof(addr)) != 0) well_panic("MySQL: connect failed");
#else
    if (connect(c->fd, (struct sockaddr *)&addr, sizeof(addr)) != 0) well_panic("MySQL: connect failed");
#endif
    c->seq = 0;
    if (my_auth(c, user, password, db) != 0) well_panic("MySQL: auth failed");
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
    my_connect_conn(&g_default, host, port, user, password, db);
}

static int my_check_ok(unsigned char *pkt, size_t plen, long long *affected) {
    if (plen < 1) return -1;
    if (pkt[0] == 0xFF) {
        char msg[512]; size_t n = plen > 3 ? plen - 3 : 0; if (n > sizeof(msg)-1) n = sizeof(msg)-1;
        memcpy(msg, pkt + 3, n); msg[n] = '\0'; well_panic(msg);
    }
    if (pkt[0] == 0x00) {
        size_t pos = 1; unsigned long long a;
        if (my_read_lenenc(pkt, plen, &pos, &a) == 0 && affected) *affected = (long long)a;
        return 0;
    }
    return -1;
}

int well_mysql_conn_exec(WellMysqlConn *c, const char *sql) {
    unsigned char *pkt = NULL; size_t plen = 0; long long affected = 0;
    size_t n = strlen(sql); unsigned char *q = (unsigned char *)malloc(n + 1);
    if (!c->connected) well_panic("MySQL: not connected");
    q[0] = 0x03; memcpy(q + 1, sql, n); c->seq = 0;
    if (my_write_packet(c, q, n + 1) != 0) { free(q); well_panic("MySQL: send failed"); }
    free(q);
    if (my_read_packet(c, &pkt, &plen) != 0) well_panic("MySQL: read failed");
    if (my_check_ok(pkt, plen, &affected) != 0) { free(pkt); well_panic("MySQL: exec failed"); }
    free(pkt); return (int)affected;
}

WellString *well_mysql_conn_query_json(WellMysqlConn *c, const char *sql) {
    unsigned char *pkt = NULL; size_t plen = 0, pos;
    unsigned long long cols = 0;
    char **names = NULL;
    WellString *out;
    size_t cap = 1024, len = 0; char *buf = (char *)malloc(cap);
    unsigned long long col;
    if (!c->connected) well_panic("MySQL: not connected");    { size_t n = strlen(sql); unsigned char *q = (unsigned char *)malloc(n + 1); q[0]=0x03; memcpy(q+1,sql,n); c->seq=0; my_write_packet(c, q,n+1); free(q); }
    if (my_read_packet(c, &pkt, &plen) != 0) well_panic("MySQL: read failed");
    if (pkt[0] == 0xFF) { char msg[512]; size_t n=plen>3?plen-3:0; if(n>sizeof(msg)-1)n=sizeof(msg)-1; memcpy(msg,pkt+3,n); msg[n]='\0'; free(pkt); well_panic(msg); }
    pos = 0;
    if (my_read_lenenc(pkt, plen, &pos, &cols) != 0) { free(pkt); well_panic("MySQL: bad column count"); }
    free(pkt);
    names = (char **)calloc((size_t)cols, sizeof(char *));
    if (!names) well_panic("out of memory (MySQL columns)");
    for (col = 0; col < cols; col++) {
        size_t pp = 0, k; unsigned long long x;
        if (my_read_packet(c, &pkt, &plen) != 0) well_panic("MySQL: read column");
        for (k = 0; k < 4; k++) { my_read_lenenc(pkt, plen, &pp, &x); pp += (size_t)x; }
        { int isnull; char *name = my_read_lenenc_str(pkt, plen, &pp, &isnull); names[col] = name; }
        free(pkt);
    }
    if (my_read_packet(c, &pkt, &plen) != 0) well_panic("MySQL: read EOF");
    free(pkt);
    buf[len++] = '['; buf[len] = '\0';
    {
        int first = 1;
        for (;;) {
            size_t pp = 0;
            if (my_read_packet(c, &pkt, &plen) != 0) well_panic("MySQL: read row");
            if (plen > 0 && pkt[0] == 0xFE && plen < 9) { free(pkt); break; }
            if (!first) { buf[len++] = ','; buf[len] = '\0'; }
            first = 0;
            buf[len++] = '{'; buf[len] = '\0';
            for (col = 0; col < cols; col++) {
                int isnull = 0; char *val = my_read_lenenc_str(pkt, plen, &pp, &isnull);
                size_t j;
                if (col) { buf[len++] = ','; }
                buf[len++] = '"';
                for (j = 0; names[col] && names[col][j]; j++) { char ch = names[col][j]; if (ch == '"' || ch == '\\') buf[len++] = '\\'; buf[len++] = ch; }
                buf[len++] = '"'; buf[len++] = ':';
                if (isnull) { memcpy(buf + len, "null", 4); len += 4; }
                else {
                    buf[len++] = '"';
                    for (j = 0; val && val[j]; j++) {
                        char ch = val[j];
                        if (ch == '"' || ch == '\\') buf[len++] = '\\';
                        if (ch == '\n') { buf[len++] = '\\'; ch = 'n'; }
                        else if (ch == '\r') { buf[len++] = '\\'; ch = 'r'; }
                        else if (ch == '\t') { buf[len++] = '\\'; ch = 't'; }
                        buf[len++] = ch;
                    }
                    buf[len++] = '"';
                }
                free(val);
                if (len + 64 >= cap) { cap *= 2; buf = (char *)realloc(buf, cap); if (!buf) well_panic("out of memory (MySQL json)"); }
            }
            buf[len++] = '}'; buf[len] = '\0';
            free(pkt);
            if (len + 64 >= cap) { cap *= 2; buf = (char *)realloc(buf, cap); if (!buf) well_panic("out of memory (MySQL json)"); }
        }
    }
    buf[len++] = ']'; buf[len] = '\0';
    out = well_string_new(buf);
    free(buf);
    for (col = 0; col < cols; col++) free(names[col]);
    free(names);
    return out;
}

int well_mysql_exec(const char *sql) {
    return well_mysql_conn_exec(&g_default, sql);
}

WellString *well_mysql_query_json(const char *sql) {
    return well_mysql_conn_query_json(&g_default, sql);
}

void well_mysql_conn_close(WellMysqlConn *c) {
    if (!c) return;
    if (c->connected) { my_close(c->fd); c->connected = 0; }
}

void well_mysql_close(void) {
    well_mysql_conn_close(&g_default);
}

/* ---------- connection pool ---------- */
struct WellMysqlPool {
    WellMutex lock;
    char *host, *user, *password, *db;
    int port;
    int min_conn, max_conn;
    WellMysqlConn **idle;
    int idle_n, idle_cap;
    int total, active;
    int closing;
};

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
    }
    return p;
}

WellMysqlConn *well_mysql_pool_acquire(WellMysqlPool *pool, int timeout_ms) {
    long long deadline = timeout_ms < 0 ? -1 : well_mono_ms() + timeout_ms;
    if (!pool) return NULL;
    for (;;) {
        WellMysqlConn *c = NULL;
        well_mutex_lock(&pool->lock);
        if (pool->closing) { well_mutex_unlock(&pool->lock); return NULL; }
        if (pool->idle_n > 0) {
            c = pool->idle[--pool->idle_n];
            pool->active++;
            well_mutex_unlock(&pool->lock);
            return c;
        }
        if (pool->total < pool->max_conn) {
            pool->total++;
            pool->active++;
            well_mutex_unlock(&pool->lock);
            c = well_mysql_conn_new(pool->host, pool->port, pool->user, pool->password, pool->db);
            if (!c) {
                well_mutex_lock(&pool->lock);
                pool->total--;
                pool->active--;
                well_mutex_unlock(&pool->lock);
                return NULL;
            }
            return c;
        }
        well_mutex_unlock(&pool->lock);
        if (deadline >= 0 && well_mono_ms() >= deadline) return NULL;
        well_sleep_ms(5);
    }
}

void well_mysql_pool_release(WellMysqlPool *pool, WellMysqlConn *conn) {
    if (!pool || !conn) return;
    well_mutex_lock(&pool->lock);
    pool->active--;
    if (pool->closing || !conn->connected) {
        pool->total--;
        well_mutex_unlock(&pool->lock);
        well_mysql_conn_close(conn);
        free(conn);
        return;
    }
    pool->idle[pool->idle_n++] = conn;
    well_mutex_unlock(&pool->lock);
}

int well_mysql_pool_exec(WellMysqlPool *pool, const char *sql) {
    WellMysqlConn *c = well_mysql_pool_acquire(pool, 3000);
    int r;
    if (!c) well_panic("MySQL pool: acquire timeout");
    r = well_mysql_conn_exec(c, sql);
    well_mysql_pool_release(pool, c);
    return r;
}

WellString *well_mysql_pool_query_json(WellMysqlPool *pool, const char *sql) {
    WellMysqlConn *c = well_mysql_pool_acquire(pool, 3000);
    WellString *r;
    if (!c) well_panic("MySQL pool: acquire timeout");
    r = well_mysql_conn_query_json(c, sql);
    well_mysql_pool_release(pool, c);
    return r;
}

void well_mysql_pool_close(WellMysqlPool *pool) {
    int i;
    if (!pool) return;
    well_mutex_lock(&pool->lock);
    pool->closing = 1;
    for (i = 0; i < pool->idle_n; i++) {
        well_mysql_conn_close(pool->idle[i]);
        free(pool->idle[i]);
    }
    pool->total -= pool->idle_n;
    pool->idle_n = 0;
    well_mutex_unlock(&pool->lock);
    while (1) {
        int active;
        well_mutex_lock(&pool->lock);
        active = pool->active;
        well_mutex_unlock(&pool->lock);
        if (active == 0) break;
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

static void well_mysql_module_shutdown(void) { well_mysql_pool_close_global(); well_mysql_close(); }
WellModule well_mysql_module = { "mysql", NULL, NULL, NULL, well_mysql_module_shutdown };

