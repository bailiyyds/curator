// Package example demonstrates source-only documentation.
package example

// Worker processes integer inputs.
type Worker struct{}

// Run returns a normalized value.
func (worker *Worker) Run(value int) int {
	return Normalize(value)
}
