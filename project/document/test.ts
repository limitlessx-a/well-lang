

enum UserEventName {
	USER_LOGIN_REQUEST:String = "USER_LOGIN_REQUEST";
	USER_LOGIN_REQUEST;//如果不写默认 = "USER_LOGIN_REQUEST"
}


如此的话, 是不是以为这可以去掉枚举的定义, 直接使用 Event 注册, 这样省去了绑定关系?

event UserLoginRequestEvent extends Event{
	id:int = 0;
	userName:String ;
	userVo:UserVo;
}

event UserLoginResponseEvent extends Event{
	id:int = 0;
	userName:String ;
	userVo:UserVo;
}

controller UserController extends Controller {
	run(){
		super.run();
		start(UserNewController);//启动子控制器
		
		on(UserLoginRequestEvent,UserLoginRequestHandler);//监听事件
		emit(new UserLoginResponseEvent());//发送事件
	}
}

如此的话, 是不是以为这可以去掉枚举的定义, 直接使用 Event 注册, 这样省去了绑定关系?
event PingEvent extends Event {
    n: int;
}

event PongEvent extends Event {
    n: int;
}

controller MainController extends Controller {
    run() {
        on(PingEvent, PingHandler);
        emit(new PongEvent(7) { n: 1 });
    }
}




enum EventType { USER_LOGIN_REQ, USER_LOGIN_RSP }
event CommonEvent extends Event {
    type:EventType;
    data:any;
}
on(EventType.USER_LOGIN_REQ, handler)
emit(new CommonEvent(EventType.USER_LOGIN_REQ, payload))



//这个关于 scope global 还是 scope perOid 是不是直接放在 on() 函数中更好, 比如 on(Event, handler, oid)


//关于事件绑定和触发
controller AppController extends Controller {
    run() {
        on(ProfileEvent, ProfileHandler);// 全局单实例
        on(ProfileEvent, ProfileHandler, 42);   // 只有 oid=42 事件

        emit( new ProfileEvent(42) { n: 1 });//  on(ProfileEvent, ProfileHandler); 这个监听会触发, 但是不会触发 on(ProfileEvent, ProfileHandler, 42); 这个监听
        emit( new ProfileEvent(42) { n: 1 }, 42);//  on(ProfileEvent, ProfileHandler, 42); 这个监听会触发, 但是不会触发 on(ProfileEvent, ProfileHandler); 这个监听

        off(ProfileEvent, ProfileHandler);// 取消 on(ProfileEvent, ProfileHandler) 事件的监听
        off(ProfileEvent, ProfileHandler, 42);// 取消 on(ProfileEvent, ProfileHandler, 42) 事件的监听
    }
}


我做了重新思考, channel 实际没有意义, 因为 controller 是无状态的, 事实上它也没有 oid, handler.oid 是在执行时指定的, 也就是说 handler 本身不属于某个 oid, 因此同一个controller 启动两次也是无意义的, 帮我确认我的想法, 暂时不修改代码


on(E,H,oid) 的 oid 没有意义, 无论是否添加 oid, 最终执行的都是 H
除非一种情况是

on(E,H1,1)
on(E,H2,2)

emit(new E(1), 1);// 执行on(E,H1,1)
emit(new E(2), 2);// 执行on(E,H2,2)

但这种情况完全可以使用, 而且语义更明确
on(E1,H1)
on(E2,H2)

emit(new E(1));// 执行on(E,H1,1)
emit(new E(2));// 执行on(E,H2,2)

因此 on(E,H1,C) 本身不需要
帮我分析我的理解, 不修改代码


你说的也有道理, 那这里改为 
on(E,H)
on(E,H,channel)//注意这里是 channel, 而不是 oid
on(Name,H)
on(Name,H,channel)

off(E,H)
off(E,H,channel)
off(Name,H)
off(Name,H,channel)

emit(E)         //触发 on(E,H)
emit(E,channel) //触发 on(E,H,channel)


// handler.oid == 1
var vo = new UserVo();        // vo.rc = 1（局部）
Cache.set(vo);                // vo.rc = 2（Cache）
var child = new BVo();        // child.rc = 1（局部）
vo.ref = child;               // child.rc = 2（vo 的字段持有）
Cache.set(child);             // child.rc = 3（Cache，key=(BVo,1)）
Cache.remove(child);          // child.rc = 2（Cache 释放）

// handler 结束：
//   局部 vo -1    → vo.rc = 1（Cache 持有）
//   局部 child -1 → child.rc = 1（vo.ref 持有，不会悬垂）
//
// 之后 Cache.remove(vo) → vo.rc = 0
//   → 释放 vo 的出边 → child -1 → child.rc = 0 → 级联回收




var vo = new UserVo();        // vo.rc = 1（局部）
Cache.set(vo);                // vo.rc = 2（Cache）
var child = new BVo();        // child.rc = 1（局部）
vo.ref = child;               // child.rc = 2（vo 的字段持有）
Cache.set(child);             // child.rc = 3（Cache，key=(BVo,1)）
Cache.remove(child);          // child.rc = 2（Cache 释放）


exec_ref[oid]

对象只有三个地方可以存在, cache, 其他对象中(struct‌,Array,Map,Linked) , handler, event

因此计数可分为两类:
1 存储计数,在cache 和 其他对象中 vo.ref
2 执行计数,在handler 和 event 中 exec_ref[oid]

所以之前的引用计数可以简化
例:

//当前 handler.oid == 1
var vo = new UserVo();        // 执行计数 exec_ref[oid]++, exec_ref[1]=1;
Cache.set(vo);                // 存储计数 vo.rc++ vo.rc=1
var child = new BVo();        // 执行计数 exec_ref[oid]++, exec_ref[1]=2;
vo.child = child;               // 存储计数 child.rc++ child.rc=1
Cache.set(child);             // 存储计数 child.rc++ child.rc=2
Cache.remove(child);          // 存储计数 child.rc-- child.rc=1
vo.child = null;                // 存储计数 child.rc-- child.rc=0
vo = new UserVo();            // 执行计数 exec_ref[oid]++, exec_ref[1]=3, 被覆盖的vo 命名 vo_old.rc=1,


//handler 执行结束后 child.rc=0 , 直接回收
//vo_old 在 handler 中声明周期结束, exec_ref[oid]--; exec_ref[1]=2;
//vo_old.rc==,vo_old.rc=0,释放, 
//vo.rc==1,vo.rc=1,不释放,

handelr 中 
var vo = new UserVo();
vo = new UserVo();
