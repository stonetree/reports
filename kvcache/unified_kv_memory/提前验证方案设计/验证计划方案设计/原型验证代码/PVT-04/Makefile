CXX = g++
CXXFLAGS = -O3 -std=c++17 -pthread -Wall

TARGET = query_plan_bench
SRCS = query_plan_bench.cc query_plan_fastpath.cc

all: $(TARGET)

$(TARGET): $(SRCS)
	$(CXX) $(CXXFLAGS) -o $(TARGET) $(SRCS)

clean:
	rm -f $(TARGET) *.csv
