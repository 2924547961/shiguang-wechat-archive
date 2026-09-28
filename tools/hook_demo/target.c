/* ============================================================
 * 教学靶子:模拟"消息分发"入口
 * ------------------------------------------------------------
 * 真实微信里,每收到一条消息(文本/图片/红包/转账...)都会经过一个
 * 按 local_type 路由的统一函数。hook 住它 = 掐住所有消息的咽喉。
 * 这个 C 程序就是那个"统一入口"的最小模拟。
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

/* 基础消息类型(低位 32 bit) */
#define MSG_TEXT      1
#define MSG_IMAGE     3
#define MSG_APP       49                          /* 链接/文件/小程序/红包/转账 都归 49 */

/* 红包/转账 = 基础类型 49 + 子类型 << 32 */
#define MSG_REDPACKET (49ULL + 2001ULL * 4294967296ULL)   /* 红包 */
#define MSG_TRANSFER  (49ULL + 2000ULL * 4294967296ULL)   /* 转账 */

/* 这就是"消息分发"回调。__declspec(dllexport) 让它出现在导出表里,
   好让 Frida 按名字就能找到它的地址(等价于真实里的"查符号")。 */
__declspec(dllexport) void dispatch_message(unsigned long long local_type, const char* from_user) {
    printf("[target] dispatch: type=%llu  from=%s\n", local_type, from_user);
    /* 真实微信在这里: 按类型 -> 弹红包 UI / 写库 / 更新列表 */
}

int main(void) {
    const char* users[] = {"alice", "bob", "carol"};
    unsigned long long types[] = {
        MSG_TEXT, MSG_IMAGE, MSG_REDPACKET, MSG_APP, MSG_TRANSFER, MSG_TEXT
    };
    int n = 6, i = 0;
    printf("[target] started  pid=%d  dispatch_message@%p\n",
           (int)getpid_local(), (void*)(size_t)dispatch_message);
    fflush(stdout);
    for (;;) {
        dispatch_message(types[i % n], users[i % 3]);
        i++;
        fflush(stdout);
        SLEEP_MS(1500);
    }
}
