#include "mylib.h"

// 全局变量定义
std::string g_crash_type = "MultiThreadCrash3";
std::string g_log_dir = "log";

// 全局实例
RaceConditionDemo* g_race_demo = nullptr;
DeadlockDemo* g_deadlock_demo = nullptr;
AtomicFailDemo* g_atomic_fail_demo = nullptr;
DoubleLockDemo* g_double_lock_demo = nullptr;
volatile std::atomic<bool> g_running{true};

// ========== 工具函数实现 ==========

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

void signal_handler(int sig, siginfo_t* info, void* context) {
    std::string signal_name;
    switch (sig) {
        case SIGSEGV: signal_name = "SIGSEGV"; break;
        case SIGABRT: signal_name = "SIGABRT"; break;
        case SIGBUS:  signal_name = "SIGBUS"; break;
        case SIGFPE:  signal_name = "SIGFPE"; break;
        case SIGILL:  signal_name = "SIGILL"; break;
        default:      signal_name = "UNKNOWN"; break;
    }

    std::cout << "捕获到信号: " << signal_name << " (代码: " << sig << ")" << std::endl;

    ucontext_t* ucontext = (ucontext_t*)context;
    void* crash_address = (void*)ucontext->uc_mcontext->__ss.__pc;

    void* callstack[128];
    callstack[0] = crash_address;
    int frames = backtrace(callstack + 1, 127);
    if (frames < 0) frames = 0;
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
            log_file << "=== 多线程崩溃报告 ===" << std::endl;
            log_file << "时间: " << timestamp << std::endl;
            log_file << "平台: " << platform << std::endl;
            log_file << "崩溃类型: " << g_crash_type << "_" << signal_name << std::endl;
            log_file << "进程ID: " << getpid() << std::endl;
            log_file << "崩溃地址: " << crash_address << std::endl;
            log_file << std::endl;

            // 模块基址信息
            log_file << "=== 模块基址信息 ===" << std::endl;
            Dl_info lib_info;
            if (dladdr((void*)signal_handler, &lib_info) != 0) {
                log_file << "libmylib.dylib基址: " << lib_info.dli_fbase << std::endl;
            }
            Dl_info pthread_info;
            if (dladdr((void*)pthread_create, &pthread_info) != 0) {
                log_file << "libsystem_pthread.dylib基址: " << pthread_info.dli_fbase << std::endl;
            }
            log_file << std::endl;

            log_file << "=== 堆栈跟踪 ===" << std::endl;
            for (int i = 0; i < frames; i++) {
                log_file << "#" << i << " " << callstack[i] << " "
                         << (symbols[i] ? symbols[i] : "NULL") << std::endl;
            }

            log_file << std::endl;
            log_file << "=== 系统信息 ===" << std::endl;
            log_file << "编译时间: " << __DATE__ << " " << __TIME__ << std::endl;
            log_file << "编译器: " << __VERSION__ << std::endl;

            log_file.close();
            std::cout << "崩溃日志已保存: " << filename << std::endl;
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
    sigaction(SIGABRT, &sa, nullptr);
    sigaction(SIGBUS, &sa, nullptr);
    sigaction(SIGFPE, &sa, nullptr);
    sigaction(SIGILL, &sa, nullptr);
}

// ========== RaceConditionDemo 实现 ==========

RaceConditionDemo::~RaceConditionDemo() {
    while (head) {
        Node* next = head->next;
        delete head;
        head = next;
    }
}

void RaceConditionDemo::write_data(int id, const char* data) {
    // 故意不加锁，制造竞态条件
    Node* node = find_node(id);
    if (node) {
        // 模拟部分写入：先写入部分数据，然后切换到读线程
        strncpy(node->data, data, 128);
        // 故意添加延迟，增加竞态窗口
        std::this_thread::sleep_for(std::chrono::microseconds(1));
        // 继续写入剩余数据
        strncpy(node->data + 100, data + 100, 128);
    } else {
        // 添加新节点，也不加锁
        Node* new_node = new Node();
        new_node->id = id;
        strncpy(new_node->data, data, 256);
        new_node->next = head;
        head = new_node;
    }
}

char* RaceConditionDemo::read_data(int id) {
    // 故意不加锁，读到部分写入的数据
    Node* node = find_node(id);
    if (node) {
        return node->data;
    }
    return nullptr;
}

RaceConditionDemo::Node* RaceConditionDemo::find_node(int id) {
    Node* current = head;
    while (current) {
        if (current->id == id) {
            return current;
        }
        current = current->next;
    }
    return nullptr;
}

void race_condition_writer(int thread_id, int operations) {
    std::random_device rd;
    std::mt19937 gen(rd());
    std::uniform_int_distribution<> dis(1, 100);

    for (int i = 0; i < operations && g_running.load(); ++i) {
        int id = dis(gen) % 10 + 1;
        char data[256];
        snprintf(data, sizeof(data), "Thread_%d_Data_%d", thread_id, i);
        g_race_demo->write_data(id, data);
        std::this_thread::sleep_for(std::chrono::microseconds(1));
    }
}

void race_condition_reader(int thread_id, int operations) {
    std::random_device rd;
    std::mt19937 gen(rd());
    std::uniform_int_distribution<> dis(1, 100);

    for (int i = 0; i < operations && g_running.load(); ++i) {
        int id = dis(gen) % 10 + 1;
        char* data = g_race_demo->read_data(id);
        if (data) {
            // 验证数据完整性，故意访问可能不完整的缓冲区
            volatile char c = data[150];  // 可能读到未写入的区域
            if (c == '\0') {
                // 模拟使用读到的数据
                std::string s(data);
                if (s.length() > 200) {
                    std::cout << "Thread " << thread_id << " read valid data" << std::endl;
                }
            }
        }
        std::this_thread::sleep_for(std::chrono::microseconds(1));
    }
}

// ========== DeadlockDemo 实现 ==========

void DeadlockDemo::lock_both_same_order() {
    std::lock_guard<std::mutex> lock1(mtx1);
    std::this_thread::sleep_for(std::chrono::microseconds(10));
    std::lock_guard<std::mutex> lock2(mtx2);
    std::cout << "lock_both_same_order completed" << std::endl;
}

void DeadlockDemo::lock_both_reverse_order() {
    std::lock_guard<std::mutex> lock2(mtx2);
    std::this_thread::sleep_for(std::chrono::microseconds(10));
    std::lock_guard<std::mutex> lock1(mtx1);
    std::cout << "lock_both_reverse_order completed" << std::endl;
}

void DeadlockDemo::trigger_deadlock() {
    std::thread t1([this]() {
        while (running.load()) {
            std::lock_guard<std::mutex> lock1(mtx1);
            std::this_thread::sleep_for(std::chrono::milliseconds(1));
            std::lock_guard<std::mutex> lock2(mtx2);
            std::cout << "Thread 1 acquired both locks" << std::endl;
        }
    });

    std::thread t2([this]() {
        std::this_thread::sleep_for(std::chrono::milliseconds(5));
        while (running.load()) {
            std::lock_guard<std::mutex> lock2(mtx2);
            std::this_thread::sleep_for(std::chrono::milliseconds(1));
            std::lock_guard<std::mutex> lock1(mtx1);
            std::cout << "Thread 2 acquired both locks" << std::endl;
        }
    });

    // 等待一段时间后强制终止，造成死锁假象
    std::this_thread::sleep_for(std::chrono::seconds(2));
    std::cout << "模拟死锁发生，调用abort()" << std::endl;
    abort();
}

void deadlock_thread_func(int thread_id) {
    if (thread_id == 0) {
        g_deadlock_demo->lock_both_same_order();
    } else {
        g_deadlock_demo->lock_both_reverse_order();
    }
}

// ========== AtomicFailDemo 实现 ==========

void AtomicFailDemo::cas_operation() {
    // 模拟CAS操作失败后未正确处理
    int old_value = expected.load(std::memory_order_relaxed);
    int new_value = old_value + 1;

    // 故意制造条件让CAS失败
    std::this_thread::sleep_for(std::chrono::microseconds(1));

    // CAS失败后没有正确处理，继续使用旧值
    int expected_val = old_value;
    if (!counter.compare_exchange_strong(expected_val, new_value)) {
        // CAS失败，但未正确处理，直接使用已失效的expected值
        // 故意访问危险内存地址
        if (dangerous_ptr) {
            *dangerous_ptr = 100;  // 访问已释放的内存
        }
        // 或者访问无效的expected值对应的地址
        volatile int* ptr = (int*)(expected.load() + 0x1000);  // 访问无效地址
        *ptr = 200;
    }
}

void AtomicFailDemo::trigger_cas_fail() {
    std::random_device rd;
    std::mt19937 gen(rd());
    std::uniform_int_distribution<> dis(1, 10);

    // 初始化危险指针
    dangerous_ptr = new int(42);

    for (int i = 0; i < 100 && running.load(); ++i) {
        if (dis(gen) % 3 == 0) {
            // 故意改变expected值，导致CAS失败
            expected.store(i, std::memory_order_relaxed);
            // 释放危险指针
            delete dangerous_ptr;
            dangerous_ptr = nullptr;
        }
        cas_operation();
    }
}

void atomic_cas_thread(int thread_id, int operations) {
    for (int i = 0; i < operations && g_running.load(); ++i) {
        g_atomic_fail_demo->cas_operation();
        std::this_thread::sleep_for(std::chrono::microseconds(1));
    }
}

// ========== DoubleLockDemo 实现 ==========

void DoubleLockDemo::double_lock_operation() {
    // 故意不加锁，制造竞态条件
    // 模拟两个线程同时操作共享资源
    static int shared_data = 0;

    // 第一次读取
    int temp1 = shared_data;

    // 模拟一些计算
    std::this_thread::sleep_for(std::chrono::microseconds(1));

    // 第二次读取 - 可能与第一次读取之间有其他线程修改
    int temp2 = shared_data;

    // 验证数据一致性 - 不一致说明发生了竞态
    if (temp1 != temp2) {
        std::cout << "检测到竞态: " << temp1 << " != " << temp2 << std::endl;
        // 触发崩溃
        volatile int* ptr = (int*)0x12345678;
        *ptr = 100;
    }

    // 修改共享数据
    shared_data = temp1 + 1;
}

void DoubleLockDemo::trigger_double_lock() {
    std::vector<std::thread> threads;

    // 启动多个线程同时操作
    for (int i = 0; i < 10; i++) {
        threads.emplace_back([this]() {
            for (int j = 0; j < 100 && running.load(); j++) {
                double_lock_operation();
            }
        });
    }

    for (auto& t : threads) {
        t.join();
    }
}

void double_lock_thread(int thread_id, int operations) {
    for (int i = 0; i < operations && g_running.load(); ++i) {
        g_double_lock_demo->double_lock_operation();
        std::this_thread::sleep_for(std::chrono::microseconds(1));
    }
}