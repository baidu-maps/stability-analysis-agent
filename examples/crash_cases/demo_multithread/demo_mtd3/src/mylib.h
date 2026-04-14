#ifndef MYLIB_H
#define MYLIB_H

#include <iostream>
#include <thread>
#include <vector>
#include <atomic>
#include <memory>
#include <chrono>
#include <random>
#include <cstring>
#include <signal.h>
#include <execinfo.h>
#include <unistd.h>
#include <sys/time.h>
#include <dlfcn.h>
#include <fstream>
#include <ctime>
#include <sstream>
#include <mutex>
#include <condition_variable>
#include <queue>
#include <functional>

// 全局变量声明
extern std::string g_crash_type;
extern std::string g_log_dir;

// 工具函数
std::string get_timestamp();
std::string get_platform_name();
void signal_handler(int sig, siginfo_t* info, void* context);
void setup_crash_handler();

// ========== 多线程崩溃场景类 ==========

// 场景1: 读写竞态 - 读操作在写操作进行中时发生
class RaceConditionDemo {
private:
    struct Node {
        int id;
        char data[256];
        Node* next;
    };
    Node* head;
    std::mutex mtx;

public:
    RaceConditionDemo() : head(nullptr) {}
    ~RaceConditionDemo();

    void write_data(int id, const char* data);
    char* read_data(int id);
    Node* find_node(int id);
};

// 场景2: 死锁 - 循环等待锁
class DeadlockDemo {
private:
    std::mutex mtx1;
    std::mutex mtx2;
    std::atomic<bool> running;

public:
    DeadlockDemo() : running(true) {}
    ~DeadlockDemo() {}

    void lock_both_same_order();
    void lock_both_reverse_order();
    void trigger_deadlock();
};

// 场景3: 原子操作失败 - CAS失败后未正确处理
class AtomicFailDemo {
private:
    std::atomic<int> counter{0};
    std::atomic<int> expected{0};
    int* dangerous_ptr;
    std::atomic<bool> running;

public:
    AtomicFailDemo() : dangerous_ptr(nullptr), running(true) {}
    ~AtomicFailDemo() {
        if (dangerous_ptr) {
            delete dangerous_ptr;
        }
    }

    void cas_operation();
    void trigger_cas_fail();
};

// 场景4: 双重加锁 - 重复加锁导致死锁
class DoubleLockDemo {
private:
    std::recursive_mutex recursive_mtx;
    std::atomic<bool> running;

public:
    DoubleLockDemo() : running(true) {}
    ~DoubleLockDemo() {}

    void double_lock_operation();
    void trigger_double_lock();
};

// 全局变量
extern RaceConditionDemo* g_race_demo;
extern DeadlockDemo* g_deadlock_demo;
extern AtomicFailDemo* g_atomic_fail_demo;
extern DoubleLockDemo* g_double_lock_demo;
extern volatile std::atomic<bool> g_running;

// 线程函数
void race_condition_writer(int thread_id, int operations);
void race_condition_reader(int thread_id, int operations);
void deadlock_thread_func(int thread_id);
void atomic_cas_thread(int thread_id, int operations);
void double_lock_thread(int thread_id, int operations);

#endif // MYLIB_H