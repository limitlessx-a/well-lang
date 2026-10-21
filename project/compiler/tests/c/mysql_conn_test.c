#include "wellrt.h"
#include "well_mysql.h"

#include <assert.h>
#include <stdio.h>
#include <string.h>

int main(void) {
    WellMysqlConn *c;
    WellString *json;

    well_init();
    c = well_mysql_conn_new("127.0.0.1", 3306, "root", "db@root", "mysql");
    assert(c != NULL);

    (void)well_mysql_conn_exec(c, "SET @well_conn_test = 1");

    json = well_mysql_conn_query_json(c, "SELECT 1 AS x UNION ALL SELECT 2 AS x");
    assert(json != NULL);
    assert(strcmp(well_string_cstr(json), "[{\"x\":\"1\"},{\"x\":\"2\"}]") == 0);
    well_release(json);

    well_mysql_conn_close(c);
    well_shutdown();
    printf("mysql conn handle ok\n");
    return 0;
}
