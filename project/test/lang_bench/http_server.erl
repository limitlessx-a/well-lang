-module(http_server).
-export([main/1]).

main([PortArg]) ->
    Port = list_to_integer(atom_to_list(PortArg)),
    {ok, LS} = gen_tcp:listen(Port, [binary, {packet, raw}, {active, false},
                                     {reuseaddr, true}, {nodelay, true}]),
    accept(LS).

accept(LS) ->
    {ok, S} = gen_tcp:accept(LS),
    Pid = spawn(fun() -> conn(S) end),
    gen_tcp:controlling_process(S, Pid),
    Pid ! go,
    accept(LS).

conn(S) ->
    receive go -> loop(S) end.

loop(S) ->
    case gen_tcp:recv(S, 0, 30000) of
        {ok, Data} ->
            case binary:match(Data, <<"\r\n\r\n">>) of
                nomatch -> gen_tcp:close(S);
                _ ->
                    gen_tcp:send(S, <<"HTTP/1.1 200 OK\r\nContent-Type: text/plain; charset=utf-8\r\nContent-Length: 2\r\nConnection: keep-alive\r\n\r\nok">>),
                    loop(S)
            end;
        _ -> gen_tcp:close(S)
    end.

