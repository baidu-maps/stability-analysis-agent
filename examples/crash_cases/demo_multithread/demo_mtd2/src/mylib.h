#ifndef DATA_MANAGER_H
#define DATA_MANAGER_H

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

extern std::string g_error_type;
extern std::string g_log_dir;

std::string get_timestamp();
std::string get_platform_name();
void capture_stack_trace(const std::string& error_type);
void signal_handler(int sig, siginfo_t* info, void* context);
void setup_crash_handler();

class DataManager {
private:
    struct DataNode {
        volatile int id;
        volatile double* data;
        volatile DataNode* next;
        volatile DataNode* prev;
        volatile bool is_valid;
        volatile size_t data_size;
        volatile std::atomic<int> ref_count;
        
        DataNode(int node_id, size_t size);
        ~DataNode();
    };
    
    volatile DataNode* head;
    volatile DataNode* tail;
    volatile std::atomic<size_t> node_count;
    volatile std::atomic<bool> is_destroying;
    mutable std::mutex mtx;
    
public:
    DataManager();
    ~DataManager();
    
    void add_data(int id, size_t size);
    void remove_data(int id);
    void update_data(int id, size_t index, double value);
    double get_data(int id, size_t index);
    void clear_all();
    size_t get_node_count() const;
    
    volatile DataNode* get_head() const { return head; }
    volatile DataNode* get_tail() const { return tail; }
};

extern DataManager* g_data_manager;
extern volatile std::atomic<bool> g_running;
extern volatile std::atomic<int> g_operation_count;

void process_data_thread(int thread_id, int operations);
void memory_cleanup_thread();
void data_sync_thread();

#endif // DATA_MANAGER_H
