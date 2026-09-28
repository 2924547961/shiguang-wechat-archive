/* ============================================================
 * 教学靶子3:模拟"发消息"这条调用链
 * ------------------------------------------------------------
 * 输入一句话 -> send_message(收件人, 文本) -> 返回消息id
 * 演示: hook 住 send_message,读到那句话、改掉它、再拿返回值。
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

/* 真正的"发送"函数:把一句话发给某人,返回一个消息 id。
   真实微信里,这一步 = 文本打包成 protobuf + 写本地库 + 走长连接发服务器。 */
__declspec(dllexport) int send_message(const char* to_user, const char* text) {
    static int next_id = 1000;
    printf("[target] send_message: to=%s  text=%s\n", to_user, text);
    return ++next_id;   /* 模拟返回消息 id */
}

/* 模拟"输入框打字 + 点发送"的循环 */
int main(void) {
    const char* msgs[] = {"在吗?", "今晚有空吃饭吗", "好的收到", "帮我带杯咖啡"};
    const char* to[]   = {"alice", "bob", "carol", "alice"};
    int n = 4, i = 0;
    printf("[target] started pid=%d  send_message@%p\n",
           (int)getpid_local(), (void*)(size_t)send_message);
    fflush(stdout);
    for (;;) {
        int id = send_message(to[i % n], msgs[i % n]);
        printf("[target]    -> 返回消息 id=%d\n", id);
        i++;
        fflush(stdout);
        SLEEP_MS(1200);
    }
}
