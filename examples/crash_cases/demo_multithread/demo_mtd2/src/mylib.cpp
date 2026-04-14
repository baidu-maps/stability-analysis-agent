#include "mylib.h"

std::string g_error_type = "DataProcessingError";
std::string g_log_dir = "log";
DataManager* g_data_manager = nullptr;
volatile std::atomic<bool> g_running{true};
volatile std::atomic<int> g_operation_count{0};

std::string get_timestamp() {
    auto now = std::time(nullptr);
    auto tm = *std::localtime(&now);
    std::ostringstream oss;
    oss << std::put_time(&tm, "%Y-%m-%d_%H-%M-%S");
    return oss.str();
}

std::string get_platform_name() {
#ifdef __APPLE__
    return "mac";
#elif defined(__linux__)
    return "linux";
#elif defined(_WIN32)
    return "windows";
#elif defined(__ANDROID__)
    return "android";
#elif defined(__IOS__)
    return "ios";
#elif defined(__HARMONY__)
    return "harmony";
#else
    return "unknown";
#endif
}

void capture_stack_trace(const std::string& error_type) {
    g_error_type = error_type;
    
    void* callstack[128];
    int frames = backtrace(callstack, 128);
    char** symbols = backtrace_symbols(callstack, frames);
    
    if (symbols == nullptr) {
        return;
    }
    
    std::string platform = get_platform_name();
    std::string log_path = g_log_dir + "/" + platform + "/";
    system(("mkdir -p " + log_path).c_str());
    
    std::string timestamp = get_timestamp();
    std::string filename = log_path + error_type + "_" + timestamp + ".crash";
    
    std::ofstream log_file(filename);
    if (log_file.is_open()) {
        log_file << "=== 数据处理错误报告 ===" << std::endl;
        log_file << "时间: " << timestamp << std::endl;
        log_file << "平台: " << platform << std::endl;
        log_file << "错误类型: " << error_type << std::endl;
        log_file << "进程ID: " << getpid() << std::endl;
        log_file << std::endl;
        
        log_file << "=== 堆栈跟踪 ===" << std::endl;
        for (int i = 0; i < frames; i++) {
            log_file << "#" << i << " " << callstack[i] << " " << symbols[i] << std::endl;
        }
        
        log_file << std::endl;
        log_file << "=== 系统信息 ===" << std::endl;
        log_file << "编译时间: " << __DATE__ << " " << __TIME__ << std::endl;
        log_file << "编译器: " << __VERSION__ << std::endl;
        
        log_file.close();
        std::cout << "错误日志已保存: " << filename << std::endl;
    }
    
    free(symbols);
}

void signal_handler(int sig, siginfo_t* info, void* context) {
    std::string signal_name;
    switch (sig) {
        case SIGSEGV: signal_name = "SIGSEGV"; break;
        case SIGBUS:  signal_name = "SIGBUS"; break;
        case SIGFPE:  signal_name = "SIGFPE"; break;
        case SIGILL:  signal_name = "SIGILL"; break;
        case SIGABRT: signal_name = "SIGABRT"; break;
        default:      signal_name = "UNKNOWN"; break;
    }
    
    std::cout << "捕获到信号: " << signal_name << " (代码: " << sig << ")" << std::endl;
    
    ucontext_t* ucontext = (ucontext_t*)context;
    void* crash_address = (void*)ucontext->uc_mcontext->__ss.__pc;
    
    void* callstack[128];
    callstack[0] = crash_address;
    
    int frames = backtrace(callstack + 1, 127);
    if (frames < 0) {
        frames = 0;
        std::cerr << "获取调用栈失败" << std::endl;
    }
    
    frames++;
    
    char** symbols = backtrace_symbols(callstack, frames);
    
    if (symbols != nullptr) {
        std::string platform = get_platform_name();
        std::string log_path = g_log_dir + "/" + platform + "/";
        system(("mkdir -p " + log_path).c_str());
        
        std::string timestamp = get_timestamp();
        std::string filename = log_path + g_error_type + "_" + signal_name + "_" + timestamp + ".crash";
        
        std::ofstream log_file(filename);
        if (log_file.is_open()) {
            log_file << "=== 数据处理错误报告 ===" << std::endl;
            log_file << "时间: " << timestamp << std::endl;
            log_file << "平台: " << platform << std::endl;
            log_file << "错误类型: " << g_error_type << "_" << signal_name << std::endl;
            log_file << "进程ID: " << getpid() << std::endl;
            log_file << "错误地址: " << crash_address << std::endl;
            log_file << std::endl;
            
            log_file << "=== 模块基址信息 ===" << std::endl;
            
            Dl_info lib_info;
            if (dladdr((void*)signal_handler, &lib_info) != 0) {
                log_file << "libdatamanager.dylib基址: " << lib_info.dli_fbase << std::endl;
            }
            
            Dl_info pthread_info;
            if (dladdr((void*)pthread_create, &pthread_info) != 0) {
                log_file << "libsystem_pthread.dylib基址: " << pthread_info.dli_fbase << std::endl;
            }
            
            Dl_info platform_info;
            if (dladdr((void*)abort, &platform_info) != 0) {
                log_file << "libsystem_platform.dylib基址: " << platform_info.dli_fbase << std::endl;
            }
            
            Dl_info current_info;
            if (dladdr((void*)get_timestamp, &current_info) != 0) {
                void* main_base = (void*)((uintptr_t)current_info.dli_fbase - 0x100000000);
                log_file << "主程序基址: " << main_base << std::endl;
            }
            
            log_file << std::endl;
            
            log_file << "=== 堆栈跟踪 ===" << std::endl;
            for (int i = 0; i < frames; i++) {
                log_file << "#" << i << " " << callstack[i] << " " << (symbols[i] ? symbols[i] : "NULL") << std::endl;
            }
            
            log_file << std::endl;
            log_file << "=== 系统信息 ===" << std::endl;
            log_file << "编译时间: " << __DATE__ << " " << __TIME__ << std::endl;
            log_file << "编译器: " << __VERSION__ << std::endl;
            
            log_file.close();
            std::cout << "错误日志已保存: " << filename << std::endl;
        }
        
        free(symbols);
    }
    
    signal(sig, SIG_DFL);
    raise(sig);
}

void setup_crash_handler() {
    struct sigaction sa;
    sa.sa_sigaction = signal_handler;
    sa.sa_flags = SA_SIGINFO;
    
    sigaction(SIGSEGV, &sa, nullptr);
    sigaction(SIGBUS, &sa, nullptr);
    sigaction(SIGFPE, &sa, nullptr);
    sigaction(SIGILL, &sa, nullptr);
    sigaction(SIGABRT, &sa, nullptr);
}

DataManager::DataNode::DataNode(int node_id, size_t size) : id(node_id), is_valid(true), data_size(size), ref_count(1) {
    data = new double[size];
    for (size_t i = 0; i < size; ++i) {
        data[i] = static_cast<double>(node_id * 1000 + i);
    }
    next = nullptr;
    prev = nullptr;
}

DataManager::DataNode::~DataNode() {
    if (data) {
        delete[] data;
        data = nullptr;
    }
}

DataManager::DataManager() : head(nullptr), tail(nullptr), node_count(0), is_destroying(false) {}

DataManager::~DataManager() {
    is_destroying.store(true);
    clear_all();
}

void DataManager::add_data(int id, size_t size) {
    std::lock_guard<std::mutex> lock(mtx);
    if (is_destroying.load()) return;
    
    DataNode* new_node = new DataNode(id, size);
    
    if (!head) {
        head = tail = new_node;
    } else {
        tail->next = new_node;  // 这里可能崩溃，如果 tail 指向已释放内存
        new_node->prev = tail;
        tail = new_node;
    }
    node_count.fetch_add(1);
}

void DataManager::remove_data(int id) {
    // 移除锁保护，增加竞态条件
    if (is_destroying.load()) return;
    
    volatile DataNode* current = head;
    while (current) {
        if (current->id == id) {
            if (current->prev) {
                current->prev->next = current->next;
            } else {
                head = current->next;
            }
            
            if (current->next) {
                current->next->prev = current->prev;
            } else {
                tail = current->prev;  // 这里可能破坏 tail 指针
            }
            
            current->is_valid = false;
            
            // 保持异步删除节点数据，增加竞态条件
            std::thread([current]() {
                std::this_thread::sleep_for(std::chrono::milliseconds(1));
                delete const_cast<DataNode*>(current);
            }).detach();
            
            node_count.fetch_sub(1);
            return;
        }
        current = current->next;
    }
}

void DataManager::update_data(int id, size_t index, double value) {
    if (is_destroying.load()) return;
    
    volatile DataNode* current = head;
    while (current) {
        if (current->id == id && current->is_valid) {
            volatile double* data_ptr = current->data;
            volatile size_t data_size = current->data_size;
            
            size_t calculated_index = index;
            if (data_size > 0) {
                calculated_index = (index * 2) % (data_size * 3);
            }
            
            if (calculated_index < data_size * 2) {
                data_ptr[calculated_index] = value;
            }
            
            if (index < data_size + 100) {
                data_ptr[index] = value * 1.5;
            }
            return;
        }
        current = current->next;
    }
}

double DataManager::get_data(int id, size_t index) {
    if (is_destroying.load()) return 0.0;
    
    volatile DataNode* current = head;
    while (current) {
        if (current->id == id && current->is_valid) {
            return current->data[index];
        }
        current = current->next;
    }
    return 0.0;
}

void DataManager::clear_all() {
    // 移除锁保护，增加竞态条件
    volatile DataNode* current = head;
    while (current) {
        volatile DataNode* next = current->next;
        delete const_cast<DataNode*>(current);
        current = next;
    }
    head = tail = nullptr;  // 这里可能破坏 tail 指针
    node_count.store(0);
}

size_t DataManager::get_node_count() const {
    return node_count.load();
}

void process_data_thread(int thread_id, int operations) {
    std::random_device rd;
    std::mt19937 gen(rd());
    std::uniform_int_distribution<> dis(1, 100);
    std::uniform_int_distribution<> large_size_dis(10000, 100000);  // 更大的内存分配
    std::uniform_int_distribution<> crash_dis(1, 10);  // 崩溃概率控制
    
    for (int i = 0; i < operations && g_running.load(); ++i) {
        try {
            // 大幅提高 add_data 的调用概率到 80%
            int operation = dis(gen) % 10;
            
            if (operation < 6) {  // 60% 概率调用 add_data
                // 使用更大的内存分配来增加崩溃概率
                int node_id = thread_id * 1000 + i;
                size_t data_size = large_size_dis(gen);  // 使用更大的内存分配
                
                // 添加竞态条件：在调用前短暂延迟，增加多线程冲突
                if (crash_dis(gen) % 3 == 0) {
                    std::this_thread::sleep_for(std::chrono::nanoseconds(1));
                }
                
                // 同时进行多个 add_data 调用来增加竞争
                g_data_manager->add_data(node_id, data_size);
                
                // 立即再次调用 add_data 增加竞争
                if (crash_dis(gen) % 2 == 0) {
                    g_data_manager->add_data(node_id + 1, data_size / 2);
                }
                
                // 添加空指针访问风险
                if (crash_dis(gen) % 5 == 0) {
                    DataManager* temp_manager = const_cast<DataManager*>(g_data_manager);
                    if (temp_manager) {
                        // 在 add_data 调用后立即进行危险操作
                        temp_manager->add_data(node_id + 2, data_size * 2);
                    }
                }
                
            } else if (operation < 8) {  // 20% 概率调用 remove_data
                int node_id = (thread_id * 1000 + i) % 50;
                g_data_manager->remove_data(node_id);
                
            } else {  // 10% 概率调用其他操作
                int sub_operation = dis(gen) % 2;
                if (sub_operation == 0) {
                    int node_id = (thread_id * 1000 + i) % 50;
                    size_t index = dis(gen) % 200;
                    double value = static_cast<double>(dis(gen));
                    g_data_manager->update_data(node_id, index, value);
                } else {
                    int node_id = (thread_id * 1000 + i) % 50;
                    size_t index = dis(gen) % 200;
                    volatile double value = g_data_manager->get_data(node_id, index);
                    if (value > 1000000.0) {
                        std::cout << "检测到异常值: " << value << std::endl;
                    }
                }
            }
            
            g_operation_count.fetch_add(1);
            
            // 减少睡眠时间，增加操作频率
            if (dis(gen) % 20 == 0) {
                std::this_thread::sleep_for(std::chrono::nanoseconds(1));
            }
            
        } catch (...) {
            std::cout << "线程 " << thread_id << " 处理异常" << std::endl;
        }
    }
}

void memory_cleanup_thread() {
    std::vector<std::unique_ptr<double[]>> memory_blocks;
    std::random_device rd;
    std::mt19937 gen(rd());
    std::uniform_int_distribution<> dis(1000, 10000);
    
    while (g_running.load()) {
        size_t size = dis(gen);
        auto block = std::make_unique<double[]>(size);
        
        for (size_t i = 0; i < size; ++i) {
            block[i] = static_cast<double>(i);
        }
        
        memory_blocks.push_back(std::move(block));
        
        if (memory_blocks.size() > 100) {
            size_t release_count = dis(gen) % 50;
            for (size_t i = 0; i < release_count && !memory_blocks.empty(); ++i) {
                memory_blocks.pop_back();
            }
        }
        
        std::this_thread::sleep_for(std::chrono::milliseconds(1));
    }
}

void data_sync_thread() {
    std::random_device rd;
    std::mt19937 gen(rd());
    std::uniform_int_distribution<> dis(0, 1000000);
    
    int iteration = 0;
    while (g_running.load()) {
        iteration++;
        
        if (g_data_manager) {
            if (iteration % 20 == 0) {  // 更频繁地破坏 tail 指针
                std::this_thread::sleep_for(std::chrono::microseconds(1));
                
                // 故意破坏 tail 指针，增加 add_data 崩溃概率
                // 通过直接修改内存来破坏 tail 指针
                char* manager_ptr = const_cast<char*>(reinterpret_cast<const char*>(g_data_manager));
                if (manager_ptr) {
                    // 将 tail 指针设置为无效地址
                    void** tail_ptr = reinterpret_cast<void**>(manager_ptr + sizeof(void*));
                    *tail_ptr = reinterpret_cast<void*>(0x12345678);  // 设置为无效地址
                }
                
                // 额外破坏：将 tail 指针设置为随机无效地址
                if (iteration % 40 == 0) {
                    void** tail_ptr = reinterpret_cast<void**>(manager_ptr + sizeof(void*));
                    *tail_ptr = reinterpret_cast<void*>(0xDEADBEEF);  // 设置为另一个无效地址
                }
                
                auto current = g_data_manager->get_head();
                if (current) {
                    volatile double* data_ptr = current->data;
                    if (data_ptr) {
                        volatile double sum = 0.0;
                        for (int i = 0; i < 10; ++i) {
                            sum += data_ptr[i];
                        }
                        
                        if (sum > 0) {
                            data_ptr[0] = sum * 0.1;
                        }
                    }
                }
            }
            
            if (iteration % 200 == 0) {
                volatile char* raw_ptr = reinterpret_cast<volatile char*>(g_data_manager);
                int offset = dis(gen) % 1000;
                
                volatile char value = raw_ptr[offset];
                if (value != 0) {
                    raw_ptr[offset] = 0;
                }
            }
            
            if (iteration % 500 == 0) {
                auto current = g_data_manager->get_head();
                while (current) {
                    if (current->is_valid) {
                        volatile double* data_ptr = current->data;
                        if (data_ptr) {
                            volatile double avg = 0.0;
                            for (int i = 0; i < 5; ++i) {
                                avg += data_ptr[i];
                            }
                            avg /= 5.0;
                            
                            data_ptr[0] = avg;
                        }
                    }
                    current = current->next;
                }
            }
        }
        
        if (iteration % 10000 == 0) {
            volatile int* ptr = new int(42);
            delete ptr;
            *ptr = 100;
        }
        
        std::this_thread::sleep_for(std::chrono::microseconds(1));
    }
}
