CXX = g++
CXXFLAGS = -O3 -std=c++17 -pthread -Wall

TARGET = multicast_fanout_bench
SRCS = multicast_fanout_bench.cc

all: $(TARGET)

$(TARGET): $(SRCS)
	$(CXX) $(CXXFLAGS) -o $(TARGET) $(SRCS)

clean:
	rm -f $(TARGET) *.csv
