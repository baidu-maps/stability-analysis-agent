#include "mylib.h"
#include <cstdlib>

void print_usage() {
    std::cout << "=== 多线程崩溃测试程序 ===" << std::endl;
    std::cout << "用法: ./mtd3_crash_test <case_id>" << std::endl;
    std::cout << std::endl;
    std::cout << "case_id 对应的崩溃类型:" << std::endl;
    std::cout << "  1 - RaceCondition (读写竞态)" << std::endl;
    std::cout << "  2 - Deadlock (死锁)" << std::endl;
    std::cout << "  3 - AtomicFail (CAS操作失败)" << std::endl;
    std::cout << "  4 - DoubleLock (双重加锁)" << std::endl;
    std::cout << std::endl;
    std::cout << "示例:" << std::endl;
    std::cout << "  ./mtd3_crash_test 1" << std::endl;
}

int main(int argc, char* argv[]) {
    if (argc < 2) {
        print_usage();
        return 1;
    }

    int case_id = std::atoi(argv[1]);

    std::cout << "=== 多线程崩溃测试 ===" << std::endl;
    std::cout << "Case ID: " << case_id << std::endl;

    // 设置崩溃处理器
    setup_crash_handler();

    const int num_threads = 4;
    const int operations_per_thread = 500;

    switch (case_id) {
        case 1: {
            // RaceCondition - 读写竞态
            std::cout << "测试场景: RaceCondition (读写竞态)" << std::endl;
            g_crash_type = "RaceCondition";
            g_race_demo = new RaceConditionDemo();

            // 初始化一些节点
            for (int i = 1; i <= 10; i++) {
                char data[256];
                snprintf(data, sizeof(data), "Initial_Data_%d", i);
                g_race_demo->write_data(i, data);
            }

            std::vector<std::thread> threads;
            // 启动多个写线程和读线程
            for (int i = 0; i < num_threads; i++) {
                threads.emplace_back(race_condition_writer, i, operations_per_thread);
                threads.emplace_back(race_condition_reader, i, operations_per_thread);
            }

            std::this_thread::sleep_for(std::chrono::seconds(2));
            g_running.store(false);

            for (auto& t : threads) {
                t.join();
            }

            delete g_race_demo;
            g_race_demo = nullptr;
            std::cout << "RaceCondition 测试完成" << std::endl;
            break;
        }

        case 2: {
            // Deadlock - 死锁
            std::cout << "测试场景: Deadlock (死锁)" << std::endl;
            g_crash_type = "Deadlock";
            g_deadlock_demo = new DeadlockDemo();

            // 使用try-catch捕获abort信号
            try {
                g_deadlock_demo->trigger_deadlock();
            } catch (...) {
                std::cout << "捕获到异常" << std::endl;
            }

            delete g_deadlock_demo;
            g_deadlock_demo = nullptr;
            std::cout << "Deadlock 测试完成" << std::endl;
            break;
        }

        case 3: {
            // AtomicFail - CAS操作失败
            std::cout << "测试场景: AtomicFail (CAS操作失败)" << std::endl;
            g_crash_type = "AtomicFail";
            g_atomic_fail_demo = new AtomicFailDemo();

            std::vector<std::thread> threads;
            for (int i = 0; i < num_threads; i++) {
                threads.emplace_back(atomic_cas_thread, i, operations_per_thread);
            }

            std::this_thread::sleep_for(std::chrono::seconds(2));
            g_running.store(false);

            for (auto& t : threads) {
                t.join();
            }

            delete g_atomic_fail_demo;
            g_atomic_fail_demo = nullptr;
            std::cout << "AtomicFail 测试完成" << std::endl;
            break;
        }

        case 4: {
            // DoubleLock - 双重加锁
            std::cout << "测试场景: DoubleLock (双重加锁)" << std::endl;
            g_crash_type = "DoubleLock";
            g_double_lock_demo = new DoubleLockDemo();

            std::vector<std::thread> threads;
            for (int i = 0; i < num_threads; i++) {
                threads.emplace_back(double_lock_thread, i, operations_per_thread / 2);
            }

            std::this_thread::sleep_for(std::chrono::seconds(1));
            g_running.store(false);

            for (auto& t : threads) {
                t.join();
            }

            delete g_double_lock_demo;
            g_double_lock_demo = nullptr;
            std::cout << "DoubleLock 测试完成" << std::endl;
            break;
        }

        default:
            std::cout << "未知 case_id: " << case_id << std::endl;
            print_usage();
            return 1;
    }

    std::cout << "程序正常结束" << std::endl;
    return 0;
}