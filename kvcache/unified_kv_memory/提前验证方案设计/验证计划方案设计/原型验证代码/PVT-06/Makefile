CXX = g++
CXXFLAGS = -O3 -std=c++17 -pthread -Wall

TARGET = rank_consensus_bench
SRCS = rank_consensus_bench.cc consume_eligibility.cc

all: $(TARGET)

$(TARGET): $(SRCS)
	$(CXX) $(CXXFLAGS) -o $(TARGET) $(SRCS)

clean:
	rm -f $(TARGET) *.csv
