-module(alloc).
-export([main/1]).

main([Arg]) ->
    N = case Arg of
            A when is_atom(A) -> list_to_integer(atom_to_list(A));
            A when is_list(A) -> list_to_integer(A)
        end,
    {Micros, _} = timer:tc(fun() -> alloc(N, 0) end),
    Ms = Micros / 1000.0,
    io:format("erlang alloc n=~p ms=~.1f ns_per_op=~.1f~n",
              [N, Ms, Ms * 1000000.0 / N]).

alloc(0, _) -> ok;
alloc(N, I) ->
    B = binary:copy(<<I:8>>, 64),
    _ = byte_size(B),
    alloc(N - 1, I).
