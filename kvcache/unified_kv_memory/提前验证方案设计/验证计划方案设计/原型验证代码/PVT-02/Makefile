CXX = g++
CXXFLAGS = -O3 -std=c++17 -pthread -Wall

TARGET = async_dag_bench
SRCS = async_dag_bench.cc descriptor_compiler.cc

all: $(TARGET)

$(TARGET): $(SRCS)
	$(CXX) $(CXXFLAGS) -o $(TARGET) $(SRCS)

clean:
	rm -f $(TARGET) *.csv
