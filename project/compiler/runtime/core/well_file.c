#include "wellrt.h"
#include "well_internal.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

WellString *well_file_read(const char *path) {
    FILE *f;
    long n;
    char *buf;
    WellString *s;
    if (!path) well_panic("File.read(null path)");
    f = fopen(path, "rb");
    if (!f) well_panic("File.read: cannot open file");
    if (fseek(f, 0, SEEK_END) != 0) {
        fclose(f);
        well_panic("File.read: seek failed");
    }
    n = ftell(f);
    if (n < 0) {
        fclose(f);
        well_panic("File.read: tell failed");
    }
    rewind(f);
    buf = (char *)malloc((size_t)n + 1);
    if (!buf) {
        fclose(f);
        well_panic("out of memory (File.read)");
    }
    if (n > 0 && fread(buf, 1, (size_t)n, f) != (size_t)n) {
        free(buf);
        fclose(f);
        well_panic("File.read: read failed");
    }
    fclose(f);
    buf[n] = '\0';
    s = well_string_new(buf);
    free(buf);
    return s;
}

void well_file_write(const char *path, const char *text) {
    FILE *f;
    size_t n;
    if (!path) well_panic("File.write(null path)");
    f = fopen(path, "wb");
    if (!f) well_panic("File.write: cannot open file");
    n = text ? strlen(text) : 0;
    if (n > 0 && fwrite(text, 1, n, f) != n) {
        fclose(f);
        well_panic("File.write: write failed");
    }
    if (fclose(f) != 0) well_panic("File.write: close failed");
}

int well_file_exists(const char *path) {
    FILE *f;
    if (!path) return 0;
    f = fopen(path, "rb");
    if (!f) return 0;
    fclose(f);
    return 1;
}

void well_file_remove(const char *path) {
    if (!path) return;
    if (!well_file_exists(path)) return;
    if (remove(path) != 0) well_panic("File.remove: remove failed");
}

long long well_host_now_ms(void) {
    return well_now_ms();
}

WellString *well_host_env(const char *name) {
    const char *v;
    if (!name) return NULL;
    v = getenv(name);
    return v ? well_string_new(v) : NULL;
}

void well_log_str(WellString *s) {
    if (s) fwrite(well_string_cstr(s), 1, (size_t)well_string_len(s), stderr);
}

void well_log_int(long long v) {
    fprintf(stderr, "%lld", v);
}

void well_log_bool(int v) {
    fputs(v ? "true" : "false", stderr);
}

void well_log_double(double v) {
    fprintf(stderr, "%g", v);
}

void well_log_nl(void) {
    fputc('\n', stderr);
}
