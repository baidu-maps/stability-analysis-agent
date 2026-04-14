#include <iostream>
#include <cstdlib>
#include <fstream>
#include <ctime>
#include <sstream>
#include "../common/include/my_lib.h"

std::string get_timestamp() {
    auto now = std::time(nullptr);
    auto tm = *std::localtime(&now);
    std::ostringstream oss;
    oss << std::put_time(&tm, "%Y-%m-%d_%H-%M-%S");
    return oss.str();
}

void write_execution_log(const std::string& crash_type, const std::string& message) {
    std::ofstream log_file("logs/execution_log.txt", std::ios::app);
    if (log_file.is_open()) {
        log_file << "[" << get_timestamp() << "] " << crash_type << ": " << message << std::endl;
        log_file.close();
    }
}

int main(int argc, char* argv[]) {
    if (argc < 2) {
        std::cout << "Usage: ./crash_test <case_id>" << std::endl;
        return 1;
    }

    // 设置崩溃处理器
    setup_crash_handler();
    
    int id = std::atoi(argv[1]);
    CrashType type = static_cast<CrashType>(id);
    set_current_crash_type(crash_type_name(type));

    std::cout << "Running crash case: " << crash_type_name(type) << std::endl;
    
    // 记录开始信息
    write_execution_log(crash_type_name(type), "开始执行崩溃测试");

    switch (type) {
        case CrashType::NullPtr:       
            write_execution_log("NullPtr", "即将执行空指针访问");
            crash_nullptr(); 
            break;
        case CrashType::DanglingPtr:   
            write_execution_log("DanglingPtr", "即将执行悬空指针访问");
            crash_dangling(); 
            break;
        case CrashType::OutOfBounds:   
            write_execution_log("OutOfBounds", "即将执行数组越界访问");
            crash_oob(); 
            break;
        case CrashType::DivZero:       
            write_execution_log("DivZero", "即将执行除零操作");
            crash_divzero(); 
            break;
        case CrashType::BadCast:       
            write_execution_log("BadCast", "即将执行错误类型转换");
            crash_bad_cast(); 
            break;
        case CrashType::StackOverflow: 
            write_execution_log("StackOverflow", "即将执行栈溢出");
            crash_stackoverflow(); 
            break;
        case CrashType::Abort:         
            write_execution_log("Abort", "即将执行主动终止");
            crash_abort(); 
            break;
        case CrashType::SigBus:
            write_execution_log("SigBus", "即将触发 SIGBUS");
            crash_sigbus();
            break;
        case CrashType::SigIll:
            write_execution_log("SigIll", "即将触发 SIGILL");
            crash_sigill();
            break;
        case CrashType::DoubleFree:
            write_execution_log("DoubleFree", "即将触发 double free");
            crash_double_free();
            break;
        case CrashType::NullFuncPtr:
            write_execution_log("NullFuncPtr", "即将触发空函数指针调用");
            crash_null_func_ptr();
            break;
        default:
            std::cout << "Unknown case id" << std::endl;
            write_execution_log("Unknown", "未知的崩溃类型");
            break;
    }

    return 0;
}
