-module(ws_server).
-export([main/1]).

-define(GUID, <<"258EAFA5-E914-47DA-95CA-C5AB0DC85B11">>).

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
    receive go -> ok end,
    case gen_tcp:recv(S, 0, 30000) of
        {ok, Head} ->
            case binary:match(Head, <<"\r\n\r\n">>) of
                nomatch -> gen_tcp:close(S);
                _ ->
                    Key = header_key(Head),
                    Accept = base64:encode(crypto:hash(sha, <<Key/binary, ?GUID/binary>>)),
                    Resp = <<"HTTP/1.1 101 Switching Protocols\r\n"
                             "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                             "Sec-WebSocket-Accept: ", Accept/binary, "\r\n\r\n">>,
                    gen_tcp:send(S, Resp),
                    gen_tcp:send(S, frame(1, <<"ready">>)),
                    loop(S)
            end;
        _ -> gen_tcp:close(S)
    end.

header_key(Head) ->
    Lines = binary:split(Head, <<"\r\n">>, [global]),
    find_key(Lines).

find_key([]) -> <<>>;
find_key([Line | Rest]) ->
    case binary:split(Line, <<":">>) of
        [Name, Value] ->
            case string:lowercase(binary_to_list(Name)) of
                "sec-websocket-key" -> trim(Value);
                _ -> find_key(Rest)
            end;
        _ -> find_key(Rest)
    end.

trim(B) -> list_to_binary(string:trim(binary_to_list(B))).

loop(S) ->
    case gen_tcp:recv(S, 2, 30000) of
        {ok, <<B0:8, B1:8>>} ->
            Op = B0 band 15,
            Masked = (B1 band 128) =/= 0,
            Len0 = B1 band 127,
            case read_len(S, Len0) of
                {ok, Len} ->
                    case read_mask(S, Masked) of
                        {ok, Mask} ->
                            case recv_exact(S, Len) of
                                {ok, Payload0} ->
                                    Payload = unmask(Payload0, Mask, 0),
                                    case Op of
                                        8 -> gen_tcp:close(S);
                                        9 -> gen_tcp:send(S, frame(10, Payload)), loop(S);
                                        _ -> gen_tcp:send(S, frame(1, Payload)), loop(S)
                                    end;
                                _ -> gen_tcp:close(S)
                            end;
                        _ -> gen_tcp:close(S)
                    end;
                _ -> gen_tcp:close(S)
            end;
        _ -> gen_tcp:close(S)
    end.

read_len(S, 126) ->
    case recv_exact(S, 2) of
        {ok, <<N:16>>} -> {ok, N};
        _ -> error
    end;
read_len(S, 127) ->
    case recv_exact(S, 8) of
        {ok, <<N:64>>} -> {ok, N};
        _ -> error
    end;
read_len(_S, N) -> {ok, N}.

read_mask(_S, false) -> {ok, <<0,0,0,0>>};
read_mask(S, true) -> recv_exact(S, 4).

recv_exact(_S, 0) -> {ok, <<>>};
recv_exact(S, N) -> gen_tcp:recv(S, N, 30000).

unmask(Bin, <<M0, M1, M2, M3>>, Off) ->
    Mask = <<M0, M1, M2, M3>>,
    unmask(Bin, Mask, Off, <<>>).

unmask(<<>>, _Mask, _Off, Acc) -> Acc;
unmask(<<B:8, Rest/binary>>, Mask, Off, Acc) ->
    M = binary:at(Mask, Off rem 4),
    unmask(Rest, Mask, Off + 1, <<Acc/binary, (B bxor M):8>>).

frame(Op, Payload) ->
    Len = byte_size(Payload),
    case Len of
        L when L < 126 -> <<1:1, 0:3, Op:4, 0:1, L:7, Payload/binary>>;
        L when L < 65536 -> <<1:1, 0:3, Op:4, 0:1, 126:7, L:16, Payload/binary>>;
        L -> <<1:1, 0:3, Op:4, 0:1, 127:7, L:64, Payload/binary>>
    end.
