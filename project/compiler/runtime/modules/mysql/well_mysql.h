#ifndef WELL_MYSQL_H
#define WELL_MYSQL_H

#include "wellrt.h"

typedef struct WellMysqlConn WellMysqlConn;
WellMysqlConn *well_mysql_conn_new(const char *host, int port, const char *user,
                                   const char *password, const char *db);
int well_mysql_conn_exec(WellMysqlConn *conn, const char *sql);
WellString *well_mysql_conn_query_json(WellMysqlConn *conn, const char *sql);
void well_mysql_conn_close(WellMysqlConn *conn);

typedef struct WellMysqlPool WellMysqlPool;
WellMysqlPool *well_mysql_pool_new(const char *host, int port,
                                   const char *user, const char *password,
                                   const char *db, int min_conn, int max_conn);
WellMysqlConn *well_mysql_pool_acquire(WellMysqlPool *pool, int timeout_ms);
void well_mysql_pool_release(WellMysqlPool *pool, WellMysqlConn *conn);
int well_mysql_pool_exec(WellMysqlPool *pool, const char *sql);
WellString *well_mysql_pool_query_json(WellMysqlPool *pool, const char *sql);
void well_mysql_pool_close(WellMysqlPool *pool);
void well_mysql_pool_configure(const char *host, int port, const char *user,
                               const char *password, const char *db,
                               int min_conn, int max_conn);
int well_mysql_pool_exec_global(const char *sql);
WellString *well_mysql_pool_query_json_global(const char *sql);
void well_mysql_pool_close_global(void);

void well_mysql_connect(const char *host, int port, const char *user,
                        const char *password, const char *db);
int well_mysql_exec(const char *sql);
WellString *well_mysql_query_json(const char *sql);
void well_mysql_close(void);

extern WellModule well_mysql_module;

#endif
