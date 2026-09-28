/* ============================================================
 * 进阶靶子:分发器 -> 具体处理器(红包/转账)
 * ------------------------------------------------------------
 * 更接近真实:分发函数按类型路由到"红包处理器"/"转账处理器"。
 * 用于演示: 1) 把钩子挂到"具体处理函数"上(而不是总入口);
 *           2) 命中一次后"撤销钩子"(detach),后续不再拦截。
 * ============================================================ */
#include <stdio.h>
#include <stdlib.h>
#ifdef _WIN32
  #include <windows.h>
  #include <process.h>
  #define SLEEP_MS(ms) Sleep(ms)
  #define getpid_local() _getpid()
#else
  #include <unistd.h>
  #define SLEEP_MS(ms) usleep((ms) * 1000)
  #define getpid_local() getpid()
#endif

#define MSG_TEXT      1
#define MSG_IMAGE     3
#define MSG_REDPACKET (49ULL + 2001ULL * 4294967296ULL)   /* 红包 */
#define MSG_TRANSFER  (49ULL + 2000ULL * 4294967296ULL)   /* 转账 */

/* 具体的红包处理器 / 转账处理器(各自一个独立函数,便于"挂到它上面") */
__declspec(dllexport) void handle_redpacket(unsigned long long local_type, const char* from_user) {
    printf("[target] handle_redpacket  type=%llu from=%s\n", local_type, from_user);
}
__declspec(dllexport) void handle_transfer(unsigned long long local_type, const char* from_user) {
    printf("[target] handle_transfer   type=%llu from=%s\n", local_type, from_user);
}
__declspec(dllexport) void dispatch_message(unsigned long long local_type, const char* from_user) {
    if (local_type == MSG_REDPACKET)
        handle_redpacket(local_type, from_user);
    else if (local_type == MSG_TRANSFER)
        handle_transfer(local_type, from_user);
    else
        printf("[target] dispatch(other) type=%llu from=%s\n", local_type, from_user);
}

int main(void) {
    const char* users[] = {"alice", "bob", "carol"};
    /* 红包在第 0、3 位 -> 短时间内会来两次红包,好观察"第一次被拦、第二次已撤" */
    unsigned long long types[] = {
        MSG_REDPACKET, MSG_TEXT, MSG_TRANSFER, MSG_REDPACKET, MSG_IMAGE, MSG_TRANSFER, MSG_TEXT
    };
    int n = 7, i = 0;
    printf("[target] started pid=%d redpacket@%p transfer@%p\n",
           (int)getpid_local(),
           (void*)(size_t)handle_redpacket, (void*)(size_t)handle_transfer);
    fflush(stdout);
    for (;;) {
        dispatch_message(types[i % n], users[i % 3]);
        i++;
        fflush(stdout);
        SLEEP_MS(900);
    }
}
