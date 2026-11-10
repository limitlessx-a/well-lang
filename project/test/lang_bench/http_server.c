#define WIN32_LEAN_AND_MEAN
#include <winsock2.h>
#include <ws2tcpip.h>
#include <windows.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static const char *RESP =
    "HTTP/1.1 200 OK\r\nContent-Type: text/plain; charset=utf-8\r\n"
    "Content-Length: 2\r\nConnection: keep-alive\r\n\r\nok";

static int find_header(const char *buf, int len) {
    int i;
    for (i = 0; i + 3 < len; i++) {
        if (buf[i]=='\r' && buf[i+1]=='\n' && buf[i+2]=='\r' && buf[i+3]=='\n')
            return i + 4;
    }
    return 0;
}

static DWORD WINAPI conn_thread(LPVOID arg) {
    SOCKET s = (SOCKET)(uintptr_t)arg;
    char buf[65536];
    int len = 0;
    for (;;) {
        int used;
        while ((used = find_header(buf, len)) != 0) {
            if (send(s, RESP, (int)strlen(RESP), 0) <= 0) goto done;
            len -= used;
            if (len > 0) memmove(buf, buf + used, (size_t)len);
        }
        if (len == (int)sizeof(buf)) goto done;
        {
            int n = recv(s, buf + len, (int)sizeof(buf) - len, 0);
            if (n <= 0) break;
            len += n;
        }
    }
done:
    closesocket(s);
    return 0;
}

int main(int argc, char **argv) {
    int port = argc > 1 ? atoi(argv[1]) : 18110;
    WSADATA wsa;
    SOCKET ls;
    struct sockaddr_in addr;
    if (WSAStartup(MAKEWORD(2,2), &wsa) != 0) return 1;
    ls = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
    if (ls == INVALID_SOCKET) return 1;
    int yes = 1;
    setsockopt(ls, SOL_SOCKET, SO_REUSEADDR, (const char*)&yes, sizeof(yes));
    memset(&addr, 0, sizeof(addr));
    addr.sin_family = AF_INET;
    addr.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    addr.sin_port = htons((u_short)port);
    if (bind(ls, (struct sockaddr*)&addr, sizeof(addr)) != 0) return 1;
    if (listen(ls, 128) != 0) return 1;
    for (;;) {
        SOCKET s = accept(ls, NULL, NULL);
        if (s == INVALID_SOCKET) continue;
        setsockopt(s, IPPROTO_TCP, TCP_NODELAY, (const char*)&yes, sizeof(yes));
        HANDLE h = CreateThread(NULL, 0, conn_thread, (LPVOID)(uintptr_t)s, 0, NULL);
        if (h) CloseHandle(h);
    }
}
