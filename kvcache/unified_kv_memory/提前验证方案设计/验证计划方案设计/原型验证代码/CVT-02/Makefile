CXX = g++
CXXFLAGS = -O3 -std=c++17 -pthread -Wall

TARGET = rcu_migration_bench
SRCS = rcu_migration_bench.cc

all: $(TARGET)

$(TARGET): $(SRCS)
	$(CXX) $(CXXFLAGS) -o $(TARGET) $(SRCS)

clean:
	rm -f $(TARGET) *.csv
