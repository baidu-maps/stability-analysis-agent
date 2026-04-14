#include "../include/my_lib.h"
#include <iostream>
#include <cstdlib>
#include <vector>
#include <string>
#include <typeinfo>
#include <fstream>
#include <ctime>
#include <sstream>
#include <signal.h>
#include <execinfo.h>
#include <unistd.h>
#include <sys/time.h>

// 全局变量用于存储崩溃信息
static std::string g_crash_type;
static std::string g_log_dir = "logs";

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

// 获取时间戳
std::string get_timestamp() {
    auto now = std::time(nullptr);
    auto tm = *std::localtime(&now);
    std::ostringstream oss;
    oss << std::put_time(&tm, "%Y-%m-%d_%H-%M-%S");
    return oss.str();
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
        log_file << "=== 崩溃报告 ===" << std::endl;
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
    if (frames < 0) {  // 检查backtrace是否成功
        frames = 0;
        std::cerr << "⚠️ 获取调用栈失败" << std::endl;
    }
    
    // 加上崩溃地址(即使frames=0也保持callstack[0]有效)
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
            log_file << "=== 崩溃报告 ===" << std::endl;
            log_file << "时间: " << timestamp << std::endl;
            log_file << "平台: " << platform << std::endl;
            log_file << "崩溃类型: " << g_crash_type << "_" << signal_name << std::endl;
            log_file << "进程ID: " << getpid() << std::endl;
            log_file << "崩溃地址: " << crash_address << std::endl;
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

void set_current_crash_type(const std::string& crash_type) {
    g_crash_type = crash_type;
}

// 设置崩溃处理器
void setup_crash_handler() {
    struct sigaction sa;
    sa.sa_sigaction = signal_handler;
    sa.sa_flags = SA_SIGINFO;
    
    sigaction(SIGSEGV, &sa, nullptr);  // 段错误
    sigaction(SIGBUS, &sa, nullptr);   // 总线错误
    sigaction(SIGFPE, &sa, nullptr);   // 浮点异常
    sigaction(SIGILL, &sa, nullptr);   // 非法指令
    sigaction(SIGABRT, &sa, nullptr);  // 中止信号
}

// 崩溃函数实现
void crash_nullptr() {
    std::cout << "触发空指针崩溃..." << std::endl;
    int* p = nullptr;
    *p = 42;  // 空指针写操作
}

void crash_dangling() {
    std::cout << "触发悬空指针崩溃..." << std::endl;
    int* p = new int(5);
    delete p;
    *p = 10;  // 悬空指针
}

void crash_oob() {
    std::cout << "触发数组越界崩溃..." << std::endl;
    std::vector<int> v(3);
    v[100] = 1;  // 数组越界
}

void crash_divzero() {
    std::cout << "触发除零崩溃..." << std::endl;
    int x = 1;
    int y = 0;
    int z = x / y; // 除零
    std::cout << z << std::endl;
}

class Base { 
public:
    virtual ~Base() = default; 
};
class Derived : public Base {};
class Other {
public:
    int value = 0;
};

void crash_bad_cast() {
    std::cout << "触发错误类型转换崩溃..." << std::endl;
    Base* b = new Derived();
    Other* o = dynamic_cast<Other*>(b); // 不相关类型，按预期返回 nullptr
    // 故意不做判空，稳定触发空指针解引用
    o->value = 1;
    delete b;
}

void recurse_forever() {
    recurse_forever(); // 无限递归 -> 栈溢出
}

void crash_stackoverflow() {
    std::cout << "触发栈溢出崩溃..." << std::endl;
    recurse_forever();
}

void crash_abort() {
    std::cout << "触发主动终止..." << std::endl;
    std::abort(); // 主动abort
}

void crash_sigbus() {
    std::cout << "触发 SIGBUS 崩溃..." << std::endl;
    raise(SIGBUS);
}

void crash_sigill() {
    std::cout << "触发 SIGILL 崩溃..." << std::endl;
    raise(SIGILL);
}

void crash_double_free() {
    std::cout << "触发 double free 崩溃..." << std::endl;
    int* p = static_cast<int*>(std::malloc(sizeof(int)));
    if (!p) {
        std::abort();
    }
    *p = 42;
    std::free(p);
    // 第二次释放同一块内存（未置空），用于触发堆一致性错误
    std::free(p);
}

void crash_null_func_ptr() {
    std::cout << "触发空函数指针调用崩溃..." << std::endl;
    using Func = void (*)();
    Func f = nullptr;
    f();
}

std::string crash_type_name(CrashType type) {
    switch (type) {
        case CrashType::NullPtr:       return "NullPtr";
        case CrashType::DanglingPtr:   return "DanglingPtr";
        case CrashType::OutOfBounds:   return "OutOfBounds";
        case CrashType::DivZero:       return "DivZero";
        case CrashType::BadCast:       return "BadCast";
        case CrashType::StackOverflow: return "StackOverflow";
        case CrashType::Abort:         return "Abort";
        case CrashType::SigBus:        return "SigBus";
        case CrashType::SigIll:        return "SigIll";
        case CrashType::DoubleFree:    return "DoubleFree";
        case CrashType::NullFuncPtr:   return "NullFuncPtr";
        default: return "Unknown";
    }
}
