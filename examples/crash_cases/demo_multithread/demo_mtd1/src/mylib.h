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

// 工具函数声明
std::string get_timestamp();
std::string get_platform_name();
void capture_stack_trace(const std::string& crash_type);
void signal_handler(int sig, siginfo_t* info, void* context);
void setup_crash_handler();

// 复杂数据结构类声明
class ComplexDataStructure {
private:
    struct Node {
        volatile int id;
        volatile double* data;
        volatile Node* next;
        volatile Node* prev;
        volatile bool is_valid;
        volatile size_t data_size;
        volatile std::atomic<int> ref_count;
        
        Node(int node_id, size_t size);
        ~Node();
    };
    
    volatile Node* head;
    volatile Node* tail;
    volatile std::atomic<size_t> node_count;
    volatile std::atomic<bool> is_destroying;
    mutable std::mutex mtx;
    
public:
    ComplexDataStructure();
    ~ComplexDataStructure();
    
    void add_node(int id, size_t size);
    void remove_node(int id);
    void modify_data(int id, size_t index, double value);
    double get_data(int id, size_t index);
    void clear_all();
    size_t get_node_count() const;
    
    // 用于调试和测试的公共方法
    volatile Node* get_head() const { return head; }
};

// 全局变量声明
extern ComplexDataStructure* g_shared_data;
extern volatile std::atomic<bool> g_running;
extern volatile std::atomic<int> g_operation_count;

// 线程函数声明
void worker_thread(int thread_id, int operations);
void memory_pressure_thread();
void corruption_thread();

#endif // MYLIB_H
