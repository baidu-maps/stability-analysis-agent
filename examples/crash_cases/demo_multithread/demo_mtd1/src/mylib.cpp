#include "mylib.h"

// 全局变量定义
std::string g_crash_type = "MultiThreadDataCorruption";
std::string g_log_dir = "log";
ComplexDataStructure* g_shared_data = nullptr;
volatile std::atomic<bool> g_running{true};
volatile std::atomic<int> g_operation_count{0};

// 获取时间戳
std::string get_timestamp() {
    auto now = std::time(nullptr);
    auto tm = *std::localtime(&now);
    std::ostringstream oss;
    oss << std::put_time(&tm, "%Y-%m-%d_%H-%M-%S");
    return oss.str();
}

// 获取平台名称
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

// 捕获堆栈跟踪
void capture_stack_trace(const std::string& crash_type) {
    g_crash_type = crash_type;
    
    // 获取堆栈跟踪
    void* callstack[128];
    int frames = backtrace(callstack, 128);
    char** symbols = backtrace_symbols(callstack, frames);
    
    if (symbols == nullptr) {
        return;
    }
    
    // 创建日志目录
    std::string platform = get_platform_name();
    std::string log_path = g_log_dir + "/" + platform + "/";
    system(("mkdir -p " + log_path).c_str());
    
    // 生成日志文件名
    std::string timestamp = get_timestamp();
    std::string filename = log_path + crash_type + "_" + timestamp + ".crash";
    
    // 写入崩溃日志
    std::ofstream log_file(filename);
    if (log_file.is_open()) {
        log_file << "=== 多线程数据损坏崩溃报告 ===" << std::endl;
        log_file << "时间: " << timestamp << std::endl;
        log_file << "平台: " << platform << std::endl;
        log_file << "崩溃类型: " << crash_type << std::endl;
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
        std::cout << "✅ 崩溃日志已保存: " << filename << std::endl;
    }
    
    free(symbols);
}

// 信号处理器
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
    
    std::cout << "🚨 捕获到信号: " << signal_name << " (代码: " << sig << ")" << std::endl;
    
    ucontext_t* ucontext = (ucontext_t*)context;
    void* crash_address = (void*)ucontext->uc_mcontext->__ss.__pc;
    
    void* callstack[128];
    callstack[0] = crash_address;
    
    int frames = backtrace(callstack + 1, 127);
    if (frames < 0) {
        frames = 0;
        std::cerr << "⚠️ 获取调用栈失败" << std::endl;
    }
    
    frames++;
    
    char** symbols = backtrace_symbols(callstack, frames);
    
    if (symbols != nullptr) {
        std::string platform = get_platform_name();
        std::string log_path = g_log_dir + "/" + platform + "/";
        system(("mkdir -p " + log_path).c_str());
        
        std::string timestamp = get_timestamp();
        std::string filename = log_path + g_crash_type + "_" + signal_name + "_" + timestamp + ".crash";
        
        std::ofstream log_file(filename);
        if (log_file.is_open()) {
            log_file << "=== 多线程数据损坏崩溃报告 ===" << std::endl;
            log_file << "时间: " << timestamp << std::endl;
            log_file << "平台: " << platform << std::endl;
            log_file << "崩溃类型: " << g_crash_type << "_" << signal_name << std::endl;
            log_file << "进程ID: " << getpid() << std::endl;
            log_file << "崩溃地址: " << crash_address << std::endl;
            log_file << std::endl;
            
            // 添加模块基址信息
            log_file << "=== 模块基址信息 ===" << std::endl;
            
            // 获取当前库基址
            Dl_info lib_info;
            if (dladdr((void*)signal_handler, &lib_info) != 0) {
                log_file << "libmylib.dylib基址: " << lib_info.dli_fbase << std::endl;
            }
            
            // 获取系统库基址
            Dl_info pthread_info;
            if (dladdr((void*)pthread_create, &pthread_info) != 0) {
                log_file << "libsystem_pthread.dylib基址: " << pthread_info.dli_fbase << std::endl;
            }
            
            Dl_info platform_info;
            if (dladdr((void*)abort, &platform_info) != 0) {
                log_file << "libsystem_platform.dylib基址: " << platform_info.dli_fbase << std::endl;
            }
            
            // 获取主程序基址（通过当前函数地址推算）
            Dl_info current_info;
            if (dladdr((void*)get_timestamp, &current_info) != 0) {
                // 通过当前库的基址推算主程序基址
                void* main_base = (void*)((uintptr_t)current_info.dli_fbase - 0x100000000); // 估算主程序基址
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
            std::cout << "✅ 崩溃日志已保存: " << filename << std::endl;
        }
        
        free(symbols);
    }
    
    signal(sig, SIG_DFL);
    raise(sig);
}

// 设置崩溃处理器
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

// ========== ComplexDataStructure 实现 ==========

// Node 构造函数
ComplexDataStructure::Node::Node(int node_id, size_t size) : id(node_id), is_valid(true), data_size(size), ref_count(1) {
    data = new double[size];
    for (size_t i = 0; i < size; ++i) {
        data[i] = static_cast<double>(node_id * 1000 + i);
    }
    next = nullptr;
    prev = nullptr;
}

// Node 析构函数
ComplexDataStructure::Node::~Node() {
    if (data) {
        delete[] data;
        data = nullptr;
    }
}

// ComplexDataStructure 构造函数
ComplexDataStructure::ComplexDataStructure() : head(nullptr), tail(nullptr), node_count(0), is_destroying(false) {}

// ComplexDataStructure 析构函数
ComplexDataStructure::~ComplexDataStructure() {
    is_destroying.store(true);
    clear_all();
}

// 添加节点
void ComplexDataStructure::add_node(int id, size_t size) {
    std::lock_guard<std::mutex> lock(mtx);
    if (is_destroying.load()) return;
    
    Node* new_node = new Node(id, size);
    
    if (!head) {
        head = tail = new_node;
    } else {
        tail->next = new_node;
        new_node->prev = tail;
        tail = new_node;
    }
    node_count.fetch_add(1);
}

// 删除节点
void ComplexDataStructure::remove_node(int id) {
    std::lock_guard<std::mutex> lock(mtx);
    if (is_destroying.load()) return;
    
    volatile Node* current = head;
    while (current) {
        if (current->id == id) {
            // 故意制造竞态条件：在删除过程中不立即释放内存
            if (current->prev) {
                current->prev->next = current->next;
            } else {
                head = current->next;
            }
            
            if (current->next) {
                current->next->prev = current->prev;
            } else {
                tail = current->prev;
            }
            
            // 标记为无效，但不立即删除
            current->is_valid = false;
            
            // 故意延迟释放，制造悬空指针
            // 这里模拟异步删除，给其他线程访问的机会
            std::thread([current]() {
                std::this_thread::sleep_for(std::chrono::milliseconds(1));
                delete const_cast<Node*>(current);
            }).detach();
            
            node_count.fetch_sub(1);
            return;
        }
        current = current->next;
    }
}

// 修改数据
void ComplexDataStructure::modify_data(int id, size_t index, double value) {
    // 故意不加锁，制造竞态条件
    if (is_destroying.load()) return;
    
    volatile Node* current = head;
    while (current) {
        if (current->id == id && current->is_valid) {
            // 模拟复杂的业务逻辑，隐藏越界访问
            volatile double* data_ptr = current->data;
            volatile size_t data_size = current->data_size;
            
            // 模拟计算偏移量，可能超出边界
            size_t calculated_index = index;
            if (data_size > 0) {
                // 这里故意制造一个可能越界的计算
                calculated_index = (index * 2) % (data_size * 3);  // 可能超出data_size
            }
            
            // 看起来有边界检查，但实际上可能越界
            if (calculated_index < data_size * 2) {  // 故意放宽条件
                data_ptr[calculated_index] = value;
            }
            
            // 额外的"安全"访问，实际上可能越界
            if (index < data_size + 100) {  // 故意增加偏移
                data_ptr[index] = value * 1.5;
            }
            return;
        }
        current = current->next;
    }
}

// 获取数据
double ComplexDataStructure::get_data(int id, size_t index) {
    // 故意不加锁，制造竞态条件
    if (is_destroying.load()) return 0.0;
    
    volatile Node* current = head;
    while (current) {
        if (current->id == id && current->is_valid) {
            // 故意不检查边界
            return current->data[index];
        }
        current = current->next;
    }
    return 0.0;
}

// 清空所有节点
void ComplexDataStructure::clear_all() {
    std::lock_guard<std::mutex> lock(mtx);
    volatile Node* current = head;
    while (current) {
        volatile Node* next = current->next;
        delete const_cast<Node*>(current);
        current = next;
    }
    head = tail = nullptr;
    node_count.store(0);
}

// 获取节点数量
size_t ComplexDataStructure::get_node_count() const {
    return node_count.load();
}

// ========== 线程函数实现 ==========

// 工作线程函数
void worker_thread(int thread_id, int operations) {
    std::random_device rd;
    std::mt19937 gen(rd());
    std::uniform_int_distribution<> dis(1, 100);
    
    for (int i = 0; i < operations && g_running.load(); ++i) {
        try {
            int operation = dis(gen) % 4;
            
            switch (operation) {
                case 0: {
                    // 添加节点
                    int node_id = thread_id * 1000 + i;
                    size_t data_size = dis(gen) % 100 + 10;
                    g_shared_data->add_node(node_id, data_size);
                    break;
                }
                case 1: {
                    // 删除节点
                    int node_id = (thread_id * 1000 + i) % 50; // 可能删除不存在的节点
                    g_shared_data->remove_node(node_id);
                    break;
                }
                case 2: {
                    // 修改数据
                    int node_id = (thread_id * 1000 + i) % 50;
                    size_t index = dis(gen) % 200; // 故意可能越界
                    double value = static_cast<double>(dis(gen));
                    g_shared_data->modify_data(node_id, index, value);
                    break;
                }
                case 3: {
                    // 读取数据
                    int node_id = (thread_id * 1000 + i) % 50;
                    size_t index = dis(gen) % 200; // 故意可能越界
                    volatile double value = g_shared_data->get_data(node_id, index);
                    // 使用value防止编译器优化
                    if (value > 1000000.0) {
                        std::cout << "异常值: " << value << std::endl;
                    }
                    break;
                }
            }
            
            g_operation_count.fetch_add(1);
            
            // 随机延迟，增加竞态条件的概率
            if (dis(gen) % 10 == 0) {
                std::this_thread::sleep_for(std::chrono::microseconds(dis(gen) % 100));
            }
            
        } catch (...) {
            std::cout << "线程 " << thread_id << " 捕获到异常" << std::endl;
        }
    }
}

// 内存压力测试线程
void memory_pressure_thread() {
    std::vector<std::unique_ptr<double[]>> memory_blocks;
    std::random_device rd;
    std::mt19937 gen(rd());
    std::uniform_int_distribution<> dis(1000, 10000);
    
    while (g_running.load()) {
        // 分配大量内存
        size_t size = dis(gen);
        auto block = std::make_unique<double[]>(size);
        
        // 写入一些数据
        for (size_t i = 0; i < size; ++i) {
            block[i] = static_cast<double>(i);
        }
        
        memory_blocks.push_back(std::move(block));
        
        // 随机释放一些内存
        if (memory_blocks.size() > 100) {
            size_t release_count = dis(gen) % 50;
            for (size_t i = 0; i < release_count && !memory_blocks.empty(); ++i) {
                memory_blocks.pop_back();
            }
        }
        
        std::this_thread::sleep_for(std::chrono::milliseconds(1));
    }
}

// 故意制造内存损坏的线程
void corruption_thread() {
    std::random_device rd;
    std::mt19937 gen(rd());
    std::uniform_int_distribution<> dis(0, 1000000);
    
    int iteration = 0;
    while (g_running.load()) {
        iteration++;
        
        // 模拟复杂的业务逻辑，隐藏内存损坏
        if (g_shared_data) {
            // 模拟回调函数或事件处理
            if (iteration % 100 == 0) {
                // 模拟异步事件处理，可能访问已释放的内存
                // 模拟事件处理延迟
                std::this_thread::sleep_for(std::chrono::microseconds(5));
                
                // 模拟访问链表节点
                auto current = g_shared_data->get_head();
                if (current) {
                    // 模拟复杂的业务计算
                    volatile double* data_ptr = current->data;
                    if (data_ptr) {
                        // 模拟数据验证和处理
                        volatile double sum = 0.0;
                        for (int i = 0; i < 10; ++i) {
                            sum += data_ptr[i];  // 可能越界访问
                        }
                        
                        // 模拟数据更新
                        if (sum > 0) {
                            data_ptr[0] = sum * 0.1;  // 可能访问已释放的内存
                        }
                    }
                }
            }
            
            // 模拟内存池管理错误
            if (iteration % 200 == 0) {
                // 模拟错误的内存管理
                volatile char* raw_ptr = reinterpret_cast<volatile char*>(g_shared_data);
                int offset = dis(gen) % 1000;
                
                // 模拟内存检查
                volatile char value = raw_ptr[offset];
                if (value != 0) {
                    // 模拟内存清理
                    raw_ptr[offset] = 0;
                }
            }
            
            // 模拟定时器回调中的内存访问
            if (iteration % 500 == 0) {
                // 模拟定时器中的数据处理
                auto current = g_shared_data->get_head();
                while (current) {
                    if (current->is_valid) {
                        // 模拟数据统计
                        volatile double* data_ptr = current->data;
                        if (data_ptr) {
                            volatile double avg = 0.0;
                            for (int i = 0; i < 5; ++i) {
                                avg += data_ptr[i];
                            }
                            avg /= 5.0;
                            
                            // 模拟数据更新
                            data_ptr[0] = avg;
                        }
                    }
                    current = current->next;
                }
            }
        }
        
        // 每10000次迭代强制触发崩溃
        if (iteration % 10000 == 0) {
            // 模拟访问已释放的内存
            volatile int* ptr = new int(42);
            delete ptr;
            *ptr = 100;  // 这应该触发use-after-free
        }
        
        // 减少延迟，增加崩溃概率
        std::this_thread::sleep_for(std::chrono::microseconds(1));
    }
}
